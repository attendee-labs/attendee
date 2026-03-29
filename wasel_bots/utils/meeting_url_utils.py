import re
from urllib.parse import parse_qs, urlparse, urlunparse

import tldextract


def root_domain_from_url(url):
    if not url:
        return None
    return tldextract.extract(url).registered_domain


def domain_and_subdomain_from_url(url):
    if not url:
        return None
    extract_from_url = tldextract.extract(url)
    return extract_from_url.subdomain + "." + extract_from_url.registered_domain


def parse_wasel_meeting_url(url):
    root_domain = root_domain_from_url(url)
    domain_and_subdomain = domain_and_subdomain_from_url(url)

    if root_domain == "webex.com" or "webex" in domain_and_subdomain:
        parsed_url = urlparse(url)
        if not parsed_url.scheme:
            parsed_url = urlparse(f"https://{url}")
        
        query_params = parse_qs(parsed_url.query)
        filtered_params = {}
        for key in ['MTID', 'password', 'pw', 'pwd']:
            if key in query_params:
                filtered_params[key] = query_params[key]
        
        new_query = '&'.join([f"{k}={v[0]}" for k, v in filtered_params.items()])
        normalized_url = urlunparse((
            parsed_url.scheme,
            parsed_url.netloc,
            parsed_url.path,
            parsed_url.params,
            new_query,
            ''
        ))
        
        return "webex", normalized_url

    return None, None


def parse_webex_join_url(join_url):
    parsed = urlparse(join_url)
    
    # Extract password from query parameters
    query_params = parse_qs(parsed.query)
    password = query_params.get('password', query_params.get('pw', query_params.get('pwd', [None])))[0]
    
    return {
        'meeting_url': join_url,
        'password': password
    }
