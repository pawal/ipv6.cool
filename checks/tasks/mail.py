# Copyright: 2019, NLnet Labs and the Internet.nl contributors
# SPDX-License-Identifier: Apache-2.0
import time

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings
from django.core.cache import cache
from urllib.parse import urlparse
import unbound
from cgi import parse_header
from collections import namedtuple

from . import SetupUnboundContext, shared
from .dispatcher import post_callback_hook, check_registry
from .dmarc_parser import parse as dmarc_parse
from .spf_parser import parse as spf_parse
from .. import scoring, categories, redis_id
from .. import batch, batch_shared_task
from ..models import MailTestAuth, SpfPolicyStatus, DmarcPolicyStatus


@shared_task(bind=True)
def mail_callback(self, results, addr, req_limit_id):
    category = categories.MailAuth()
    mtauth = callback(results, addr, category)
    # Always calculate scores on saving.
    from ..probes import mail_probe_auth
    mail_probe_auth.rated_results_by_model(mtauth)
    post_callback_hook(req_limit_id, self.request.id)
    return results


@batch_shared_task(bind=True)
def batch_mail_callback(self, results, addr):
    category = categories.MailAuth()
    mtauth = callback(results, addr, category)
    # Always calculate scores on saving.
    from ..probes import batch_mail_probe_auth
    batch_mail_probe_auth.rated_results_by_model(mtauth)
    batch.scheduler.batch_callback_hook(mtauth, self.request.id)


mail_registered = check_registry("mail_auth", mail_callback)
batch_mail_registered = check_registry("batch_mail_auth", batch_mail_callback)


@mail_registered
@shared_task(
    bind=True,
    soft_time_limit=settings.SHARED_TASK_SOFT_TIME_LIMIT_LOW,
    time_limit=settings.SHARED_TASK_TIME_LIMIT_LOW,
    base=SetupUnboundContext)
def dmarc(self, url, *args, **kwargs):
    return do_dmarc(self, url, *args, **kwargs)


@batch_mail_registered
@batch_shared_task(
    bind=True,
    soft_time_limit=settings.BATCH_SHARED_TASK_SOFT_TIME_LIMIT_HIGH,
    time_limit=settings.BATCH_SHARED_TASK_TIME_LIMIT_HIGH,
    base=SetupUnboundContext)
def batch_dmarc(self, url, *args, **kwargs):
    return do_dmarc(self, url, *args, **kwargs)


@mail_registered
@shared_task(
    bind=True,
    soft_time_limit=settings.SHARED_TASK_SOFT_TIME_LIMIT_LOW,
    time_limit=settings.SHARED_TASK_TIME_LIMIT_LOW,
    base=SetupUnboundContext)
def dkim(self, url, *args, **kwargs):
    return do_dkim(self, url, *args, **kwargs)


@batch_mail_registered
@batch_shared_task(
    bind=True,
    soft_time_limit=settings.BATCH_SHARED_TASK_SOFT_TIME_LIMIT_HIGH,
    time_limit=settings.BATCH_SHARED_TASK_TIME_LIMIT_HIGH,
    base=SetupUnboundContext)
def batch_dkim(self, url, *args, **kwargs):
    return do_dkim(self, url, *args, **kwargs)


@mail_registered
@shared_task(
    bind=True,
    soft_time_limit=settings.SHARED_TASK_SOFT_TIME_LIMIT_LOW,
    time_limit=settings.SHARED_TASK_TIME_LIMIT_LOW,
    base=SetupUnboundContext)
def spf(self, url, *args, **kwargs):
    return do_spf(self, url, *args, **kwargs)


@batch_mail_registered
@batch_shared_task(
    bind=True,
    soft_time_limit=settings.BATCH_SHARED_TASK_SOFT_TIME_LIMIT_HIGH,
    time_limit=settings.BATCH_SHARED_TASK_TIME_LIMIT_HIGH,
    base=SetupUnboundContext)
def batch_spf(self, url, *args, **kwargs):
    return do_spf(self, url, *args, **kwargs)


def callback(results, addr, category):
    subtests = category.subtests
    mtauth = MailTestAuth(domain=addr)
    for testname, result in results:
        if testname == 'dkim':
            dkim_available = result.get("available")
            mtauth.dkim_available = dkim_available
            mtauth.dkim_score = result.get("score")
            if dkim_available:
                subtests['dkim'].result_good()
            else:
                subtests['dkim'].result_bad()

        elif testname == 'dmarc':
            dmarc_available = result.get("available")
            dmarc_record = result.get("record")
            dmarc_score = result.get("score")
            dmarc_policy_status = result.get("policy_status")
            dmarc_policy_score = result.get("policy_score")
            mtauth.dmarc_available = dmarc_available
            mtauth.dmarc_record = dmarc_record
            mtauth.dmarc_score = dmarc_score
            mtauth.dmarc_policy_status = dmarc_policy_status
            mtauth.dmarc_policy_score = dmarc_policy_score
            if dmarc_available:
                subtests['dmarc'].result_good(dmarc_record)

                if dmarc_policy_status == DmarcPolicyStatus.valid:
                    subtests['dmarc_policy'].result_good()
                elif dmarc_policy_status == DmarcPolicyStatus.invalid_external:
                    subtests['dmarc_policy'].result_invalid_external(
                        dmarc_record)
                else:
                    if dmarc_policy_status == DmarcPolicyStatus.invalid_syntax:
                        subtests['dmarc_policy'].result_bad_syntax(
                            dmarc_record)
                    elif dmarc_policy_status == DmarcPolicyStatus.invalid_p_sp:
                        subtests['dmarc_policy'].result_bad_policy(
                            dmarc_record)

            else:
                subtests['dmarc'].result_bad(dmarc_record)

        elif testname == 'spf':
            spf_available = result.get("available")
            spf_record = result.get("record")
            spf_score = result.get("score")
            spf_policy_status = result.get("policy_status")
            spf_policy_score = result.get("policy_score")
            spf_policy_records = result.get("policy_records")
            mtauth.spf_available = spf_available
            mtauth.spf_record = spf_record
            mtauth.spf_score = spf_score
            mtauth.spf_policy_status = spf_policy_status
            mtauth.spf_policy_score = spf_policy_score
            mtauth.spf_policy_records = spf_policy_records
            if spf_available:
                subtests['spf'].result_good(spf_record)

                if spf_policy_status == SpfPolicyStatus.valid:
                    subtests['spf_policy'].result_good()
                else:
                    # Show the records in the encountered ordered.
                    # Erronoeous record last.
                    spf_records = spf_policy_records[::-1]
                    if spf_policy_status == SpfPolicyStatus.invalid_syntax:
                        subtests['spf_policy'].result_bad_syntax(
                            spf_records)
                    elif spf_policy_status == SpfPolicyStatus.max_dns_lookups:
                        subtests['spf_policy'].result_bad_max_lookups(
                            spf_records)
                    elif spf_policy_status == SpfPolicyStatus.invalid_all:
                        subtests['spf_policy'].result_bad_policy(
                            spf_records)
                    elif spf_policy_status == SpfPolicyStatus.invalid_include:
                        subtests['spf_policy'].result_bad_include(
                            spf_records)
                    elif spf_policy_status == SpfPolicyStatus.invalid_redirect:
                        subtests['spf_policy'].result_bad_redirect(
                            spf_records)

            else:
                subtests['spf'].result_bad(spf_record)

    mtauth.report = category.gen_report()
    mtauth.save()
    return mtauth


def as_txt(data):
    try:
        txt = "".join(unbound.ub_data.dname2str(data))
    except UnicodeError:
        txt = "<Non ASCII characters found>"
    return txt


def dkim_callback(data, status, r):
    data["score"] = scoring.MAIL_AUTH_DKIM_FAIL
    data["available"] = False
    if status == 0:
        available = False
        if r.rcode == unbound.RCODE_NOERROR:
            # _domainkey empty non-terminal does exist,
            # assume there is a DKIM pubkey
            available = True
            score = scoring.MAIL_AUTH_DKIM_PASS
        elif r.rcode == unbound.RCODE_NXDOMAIN:
            # we know for sure there is no DKIM pubkey
            score = scoring.MAIL_AUTH_DKIM_FAIL
        else:
            # resolving problems, servfail probably
            score = scoring.MAIL_AUTH_DKIM_ERROR
        data["score"] = score
        data["available"] = available
    data["done"] = True


def do_dkim(self, url, *args, **kwargs):
    try:
        cb_data = self.async_resolv(
            "_domainkey.{}".format(url), unbound.RR_TYPE_TXT, dkim_callback)
        result = dict(
            available="available" in cb_data and cb_data["available"],
            score=cb_data["score"])

    except SoftTimeLimitExceeded:
        result = dict(
            available=False,
            score=scoring.MAIL_AUTH_DKIM_FAIL)

    return ("dkim", result)


def spf_callback(data, status, r):
    data["score"] = scoring.MAIL_AUTH_SPF_FAIL
    data["available"] = False
    data["record"] = []
    if status == 0:
        record = []
        available = False
        if r.rcode == unbound.RCODE_NOERROR and r.havedata == 1:
            #  TXT record(s) found, start looking for SPF
            score = scoring.MAIL_AUTH_SPF_FAIL
            for d in r.data.data:
                txt = as_txt(d)
                if txt.lower().startswith("v=spf1"):
                    record.append(txt)
                    if available:
                        # We see more than one SPF record. Fail the test.
                        available = False
                        score = scoring.MAIL_AUTH_SPF_FAIL
                        break
                    else:
                        available = True
                        score = scoring.MAIL_AUTH_SPF_PASS
        elif (r.rcode == unbound.RCODE_NXDOMAIN
                or (r.rcode == unbound.RCODE_NOERROR and r.havedata == 0)):
            # we know for sure there is no SPF record
            score = scoring.MAIL_AUTH_SPF_FAIL
        else:
            # resolving problems, servfail probably
            score = scoring.MAIL_AUTH_SPF_ERROR

        data["score"] = score
        data["available"] = available
        data["record"] = record
    data["done"] = True


def resolve_spf_record(url, task):
    return task.async_resolv(
        url, unbound.RR_TYPE_TXT, callback=spf_callback)


def do_spf(self, url, *args, **kwargs):
    try:
        cb_data = resolve_spf_record(url, self)
        available = 'available' in cb_data and cb_data['available']
        score = cb_data['score']
        record = cb_data['record']
        policy_status = None
        policy_score = scoring.MAIL_AUTH_SPF_POLICY_FAIL
        policy_records = []

        if len(record) == 1:
            policy_status, policy_score, _ = spf_check_policy(
                url, record[0], self, policy_records=policy_records)

        result = dict(
            available=available,
            score=score,
            record=record,
            policy_status=policy_status,
            policy_score=policy_score,
            policy_records=policy_records)

    except SoftTimeLimitExceeded:
        result = dict(
            available=False,
            score=scoring.MAIL_AUTH_SPF_FAIL,
            record=[],
            policy_status=None,
            policy_score=scoring.MAIL_AUTH_SPF_POLICY_FAIL,
            policy_records=[])

    return ("spf", result)


def spf_check_include_redirect(
        domain, term, task, policy_records, max_lookups, assignment_operator,
        bad_status, is_include=False):
    """
    Check the 'include' and 'redirect' terms.

    If a record is found, check that record for syntax and efficiency.
    Respects the maximum DNS lookups.  Macros are not expanded and
    thus not followed.  Bad statuses are converted to the given
    `bad_status` except for the `SpfPolicyStatus.max_dns_lookups`.

    """
    left_lookups = max_lookups
    status = SpfPolicyStatus.valid
    score = scoring.MAIL_AUTH_SPF_POLICY_PASS
    if max_lookups < 1:
        status = SpfPolicyStatus.max_dns_lookups
        score = scoring.MAIL_AUTH_SPF_POLICY_PARTIAL

    if status == SpfPolicyStatus.valid:
        url = term.split(assignment_operator)[1].strip()
        # Don't expand macros.
        if '{' in url:
            return status, score, left_lookups

    if status == SpfPolicyStatus.valid:
        cb_data = resolve_spf_record(url, task)
        new_spf = cb_data['record']
        left_lookups -= 1
        if not new_spf:
            status = bad_status
            score = scoring.MAIL_AUTH_SPF_POLICY_PARTIAL

        elif len(new_spf) > 1:
            status = bad_status
            score = scoring.MAIL_AUTH_SPF_POLICY_PARTIAL
            for record in new_spf:
                policy_records.append((url, record))

    if status == SpfPolicyStatus.valid:
        new_spf = new_spf[0]
        status, score, left_lookups = spf_check_policy(
            url, new_spf, task, policy_records=policy_records,
            max_lookups=left_lookups, is_include=is_include)

    if (status != SpfPolicyStatus.valid
            and status != SpfPolicyStatus.max_dns_lookups):
        status = bad_status

    return status, score, left_lookups


def spf_check_redirect(domain, term, task, policy_records, max_lookups):
    return spf_check_include_redirect(
        domain, term, task, policy_records, max_lookups, '=',
        SpfPolicyStatus.invalid_redirect)


def spf_check_include(domain, term, task, policy_records, max_lookups):
    return spf_check_include_redirect(
        domain, term, task, policy_records, max_lookups, ':',
        SpfPolicyStatus.invalid_include, is_include=True)


def spf_check_policy(
        domain, spf_record, task, policy_records, max_lookups=10,
        is_include=False):
    """
    Check the SPF policy for syntax and efficiency.

    Terms being checked: all, include, redirect.
    Respects the maximum number of 10 DNS lookups.

    """
    left_lookups = max_lookups
    status = SpfPolicyStatus.valid
    score = scoring.MAIL_AUTH_SPF_POLICY_PASS
    parsed = spf_parse(spf_record)
    if not parsed:
        status = SpfPolicyStatus.invalid_syntax
        score = scoring.MAIL_AUTH_SPF_POLICY_FAIL

    elif not parsed.get('terms'):
        # Defaults to '?all'.
        status = SpfPolicyStatus.invalid_all
        score = scoring.MAIL_AUTH_SPF_POLICY_PARTIAL

    else:
        terms = []
        redirect_terms = []
        all_found = False
        for term in (t.lower() for t in parsed['terms']):
            if term.startswith('redirect'):
                redirect_terms.append(term)
            elif 'include:' in term:
                terms.append(term)
            elif term.endswith('all') and len(term) < 5:
                all_found = True

                # Check 'all'
                if (term.startswith(('+', 'a'))
                        or (not is_include and term.startswith('?'))):
                    status = SpfPolicyStatus.invalid_all
                    score = scoring.MAIL_AUTH_SPF_POLICY_PARTIAL
                break

        if status == SpfPolicyStatus.valid:
            # No 'all' and no 'redirect', the default is '?all', fail.
            # Not applicable to include records, as the result is just
            # not_match.
            if not is_include and not redirect_terms and not all_found:
                status = SpfPolicyStatus.invalid_all
                score = scoring.MAIL_AUTH_SPF_POLICY_PARTIAL

        if status == SpfPolicyStatus.valid:
            # Redirects must be last
            terms = terms + redirect_terms

            for term in terms:
                if status != SpfPolicyStatus.valid:
                    break

                if "redirect=" in term and not all_found:
                    status, score, left_lookups = spf_check_redirect(
                        domain, term, task, policy_records, left_lookups)
                    # Only one redirect is followed
                    break

                elif "include:" in term:
                    status, score, left_lookups = spf_check_include(
                        domain, term, task, policy_records, left_lookups)

    if status != SpfPolicyStatus.valid:
        policy_records.append((domain, spf_record))

    return status, score, left_lookups


def dmarc_callback(data, status, r):
    data["score"] = scoring.MAIL_AUTH_DMARC_FAIL
    data["available"] = False
    data["record"] = []
    if status == 0:
        record = []
        available = False
        continue_looking = False
        if r.rcode == unbound.RCODE_NOERROR and r.havedata == 1:
            # _dmarc TXT record found
            score = scoring.MAIL_AUTH_DMARC_FAIL
            for d in r.data.data:
                txt = as_txt(d)
                if txt.lower().startswith("v=dmarc1"):
                    record.append(txt)
                    if available:
                        # We see more than one DMARC record. Fail the test.
                        available = False
                        score = scoring.MAIL_AUTH_DMARC_FAIL
                        break
                    else:
                        available = True
                        score = scoring.MAIL_AUTH_DMARC_PASS
            # Check if we got an answer but all the TXT records were not DMARC
            # records. In that case look for a record in the organizational
            # domain.
            if not record:
                score = scoring.MAIL_AUTH_DMARC_FAIL
                continue_looking = True
        elif (r.rcode == unbound.RCODE_NXDOMAIN
                or (r.rcode == unbound.RCODE_NOERROR and r.havedata == 0)):
            # we know for sure there is no DMARC policy
            score = scoring.MAIL_AUTH_DMARC_FAIL
            continue_looking = True
        else:
            # resolving problems, servfail probably
            score = scoring.MAIL_AUTH_DMARC_ERROR

        data["score"] = score
        data["available"] = available
        data["record"] = record
        data["cont"] = continue_looking
    data["done"] = True


def do_dmarc(self, url, *args, **kwargs):
    try:
        cb_data = dict(cont=True)
        is_organizational_domain = False
        public_suffix_list = dmarc_get_public_suffix_list(task=self)
        if not public_suffix_list:
            # We don't have the public suffix list.
            # Raise SoftTimeLimitExceeded to easily fail the test.
            raise SoftTimeLimitExceeded

        cb_data = self.async_resolv(
            "_dmarc.{}".format(url), unbound.RR_TYPE_TXT, dmarc_callback)
        if cb_data.get("cont"):
            url = dmarc_find_organizational_domain(url, public_suffix_list)
            cb_data = self.async_resolv(
                "_dmarc.{}".format(url), unbound.RR_TYPE_TXT, dmarc_callback)
            is_organizational_domain = True

        available = 'available' in cb_data and cb_data['available']
        score = cb_data["score"]
        record = cb_data["record"]
        policy_status = None
        policy_score = scoring.MAIL_AUTH_DMARC_POLICY_FAIL

        if len(record) == 1:
            policy_status, policy_score = dmarc_check_policy(
                record[0], url, self, is_organizational_domain,
                public_suffix_list)

        result = dict(
            available=available,
            score=score,
            record=record,
            policy_status=policy_status,
            policy_score=policy_score)

    except SoftTimeLimitExceeded:
        result = dict(
            available=False,
            score=scoring.MAIL_AUTH_DMARC_FAIL,
            record=[],
            policy_status=None,
            policy_score=scoring.MAIL_AUTH_DMARC_POLICY_FAIL)

    return ("dmarc", result)


def dmarc_check_policy(
        dmarc_record, domain, task, is_organizational_domain,
        public_suffix_list):
    """
    Check the DMARC record for syntax and efficiency.

    If a `rua` and/or `ruf` directives are present and the domain
    listed is not the same as the domain being tested, verify the
    external destinations.

    """
    domain = domain.lower().rstrip('.')
    parsed = dmarc_parse(dmarc_record)
    status, score = dmarc_verify_sufficient_policy(
        parsed, is_organizational_domain, public_suffix_list)

    if status == DmarcPolicyStatus.valid and parsed.get('directives'):
        status, score = dmarc_verify_external_destinations(
            domain, parsed, task, public_suffix_list)
    return (status, score)


def dmarc_verify_sufficient_policy(
        parsed, is_organizational_domain, public_suffix_list):
    """
    Verify that the s=(sp=) policy is not 'none'.

    """
    status = DmarcPolicyStatus.valid
    score = scoring.MAIL_AUTH_DMARC_POLICY_PASS
    if not parsed:
        status = DmarcPolicyStatus.invalid_syntax
        score = scoring.MAIL_AUTH_DMARC_POLICY_FAIL

    request = None
    if status == DmarcPolicyStatus.valid:
        if not parsed.get('directives'):
            status = DmarcPolicyStatus.invalid_p_sp
            score = scoring.MAIL_AUTH_DMARC_POLICY_PARTIAL
        else:
            if is_organizational_domain:
                if (not parsed['directives'].get('srequest')
                        and not parsed['directives'].get('request')):
                    status = DmarcPolicyStatus.invalid_p_sp
                    score = scoring.MAIL_AUTH_DMARC_POLICY_PARTIAL
                else:
                    if parsed['directives'].get('srequest'):
                        request = parsed['directives']['srequest']
                    elif parsed['directives'].get('request'):
                        request = parsed['directives']['request']
            else:
                if not parsed['directives'].get('request'):
                    status = DmarcPolicyStatus.invalid_p_sp
                    score = scoring.MAIL_AUTH_DMARC_POLICY_PARTIAL
                else:
                    request = parsed['directives']['request']

            if request is not None:
                value = request.split('=')[1]
                if value.lower() == "none":
                    status = DmarcPolicyStatus.invalid_p_sp
                    score = scoring.MAIL_AUTH_DMARC_POLICY_PARTIAL
    return (status, score)


def _dmarc_get_ru_host(parsed):
    """
    Generator for returning the host for rua and ruf directives.

    """
    for directive in ('auri', 'furi'):
        if parsed['directives'].get(directive):
            value = parsed['directives'][directive].split('=')[1]
            uris = value.split(',')
            for uri in uris:
                uri = urlparse(uri)
                if uri.netloc == '' and uri.path:
                    host = uri.path
                    if '@' in host:
                        host = host.split('@', 1)[1].split('!', 1)[0]
                    host = host.strip().lower().rstrip('.')
                    yield host


def dmarc_verify_external_destinations(
        domain, parsed, task, public_suffix_list):
    """
    Verify external destinations as per section 7.1 (RFC7489).

    """
    status = DmarcPolicyStatus.valid
    score = scoring.MAIL_AUTH_DMARC_POLICY_PASS
    domain_org = dmarc_find_organizational_domain(domain, public_suffix_list)
    for host in _dmarc_get_ru_host(parsed):
        host_org = dmarc_find_organizational_domain(host, public_suffix_list)
        if domain_org != host_org:
            ext_qname = "{}._report._dmarc.{}".format(domain, host)
            txt_records = task.resolve(ext_qname, unbound.RR_TYPE_TXT)
            is_dmarc = False
            for txt in txt_records:
                ru_parsed = dmarc_parse(txt[0])
                if ru_parsed:
                    if is_dmarc:
                        # Second valid DMARC record, abort.
                        is_dmarc = False
                        break
                    # Need to check same host on rua/ruf.
                    for ru_host in _dmarc_get_ru_host(ru_parsed):
                        if host != ru_host:
                            is_dmarc = False
                            break
                    is_dmarc = True

            if not is_dmarc:
                status = DmarcPolicyStatus.invalid_external
                score = scoring.MAIL_AUTH_DMARC_POLICY_PASS
                break

    return (status, score)


def dmarc_find_organizational_domain(domain, public_suffix_list):
    """
    Find the organizational domain of the given domain.

    Uses mainly the algorithm found at https://publicsuffix.org/list/ to get
    the organizational domain. Could return "" if none is found.

    """
    # The algorithm could be more elaborate but this is simple and fast enough.
    organizational_domain = ""
    matching_rule = []
    matching_count = 0
    matching_exception = False
    labels = domain.split(".")[::-1]
    for rule, exception in public_suffix_list:
        if matching_exception and not exception:
            continue

        # We always need to have the whole rule available. zip (later) will
        # truncate to the shortest list.
        if len(rule) > len(labels):
            continue

        matched = 0
        for a, b in zip(rule, labels):
            if a in ("*", b):
                matched += 1
            else:
                break

        # The whole rule needs to match.
        if matched == len(rule):
            if exception:
                matching_rule = rule
                matching_count = matched
                matching_exception = exception
            elif matched > matching_count:
                matching_rule = rule
                matching_count = matched

    if matching_rule:
        if matching_exception:
            organizational_domain = ".".join(labels[:len(matching_rule)][::-1])
        elif len(labels) > len(matching_rule):
            organizational_domain = ".".join(labels[:matching_count+1][::-1])
    # Default matching rule is "*"
    elif len(labels) > 1:
        organizational_domain = ".".join(labels[:2][::-1])

    return organizational_domain


def dmarc_fetch_public_suffix_list(task):
    """
    Fetch the list from the configured URL and parse it leaving out comments
    empty lines and invalid lines.

    """
    def requests_like_get(url):
        scheme, netloc, path, *unused = urlparse(url)
        port = 443 if scheme == 'https' else 80
        conn, r, *unused = shared.http_fetch(host=netloc, path=path,
            port=port, keep_conn_open=True, task=task)

        rr = namedtuple('Response', ['status_code', 'text'])
        rr.status_code = r.status
        if r.status == 200:
            ct_header = r.getheader('Content-Type', None)
            if ct_header:
                encoding = parse_header(ct_header)[1]['charset']
            else:
                encoding = 'utf-8'
            rr.text = r.read().decode(encoding)

        conn.close()
        return rr

    suffix_url = settings.PUBLIC_SUFFIX_LIST_URL
    public_suffix_list = []
    r = requests_like_get(suffix_url)
    lines = r.text.split("\n")
    for line in lines:
        line = line.rstrip()
        if line and not line.startswith(("//", " ")):
            exception = False
            if line.startswith("!"):
                exception = True
                line = line[1:]
            # Convert to punnycode.
            # This is how we are going to compare domain names later.
            try:
                line = line.encode("idna").decode("ascii")
                public_suffix_list.append((line.split(".")[::-1], exception))
            except (UnicodeError, ValueError):
                pass
    return public_suffix_list


def dmarc_get_public_suffix_list(task):
    """
    Return the parsed public suffix list.

    If it is not already in cache fetch it, parse it and store it in cache.
    Also make sure that only one task does the fetching and others wait for
    the list.

    """
    public_suffix_list_id = redis_id.psl_data.id
    public_suffix_list_ttl = redis_id.psl_data.ttl
    public_suffix_list_loading_id = redis_id.psl_loading.id
    public_suffix_list_loading_ttl = redis_id.psl_loading.ttl
    tries = 3
    while True:
        public_suffix_list = cache.get(public_suffix_list_id)
        if public_suffix_list or tries <= 0:
            return public_suffix_list

        is_loading = cache.get(public_suffix_list_loading_id)
        if is_loading:
            tries -= 1
            time.sleep(3)
            continue
        break

    if not is_loading:
        status = cache.add(
            public_suffix_list_loading_id, True,
            timeout=public_suffix_list_loading_ttl)
        if status:
            public_suffix_list = dmarc_fetch_public_suffix_list(task)
            if public_suffix_list:
                cache.set(
                    public_suffix_list_id, public_suffix_list,
                    timeout=public_suffix_list_ttl)
                cache.delete(public_suffix_list_loading_id)
        else:
            # Lost the race; call this again to wait for the list.
            public_suffix_list = dmarc_get_public_suffix_list()

    return public_suffix_list
