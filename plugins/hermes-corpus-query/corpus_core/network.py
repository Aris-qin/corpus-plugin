import http.client
import ipaddress
import os
import socket
import ssl
from urllib.parse import urljoin, urlsplit, urlunsplit

from .errors import CorpusError, require


def resolve_public(host, port):
    return list(dict.fromkeys(entry[4][0] for entry in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))


def validate_url(url, resolver=resolve_public):
    require(type(url) is str and 0 < len(url) <= 4096 and not any(ord(character) < 33 for character in url),
            'UNSAFE_URL', 'invalid URL characters/length')
    try:
        parts = urlsplit(url)
        port = parts.port or 443
        host = (parts.hostname or '').encode('idna').decode('ascii')
    except (ValueError, UnicodeError) as error:
        raise CorpusError('UNSAFE_URL', 'malformed URL') from error
    require(parts.scheme == 'https' and host and port == 443 and not parts.username and not parts.password,
            'UNSAFE_URL', 'only public HTTPS on port 443 without credentials is supported')
    addresses = resolver(host, port)
    require(bool(addresses), 'UNSAFE_URL', 'DNS returned no public addresses')
    try:
        require(all(ipaddress.ip_address(address).is_global and not ipaddress.ip_address(address).is_multicast
                    for address in addresses), 'UNSAFE_URL', 'DNS must resolve exclusively to public unicast addresses')
    except ValueError as error:
        raise CorpusError('UNSAFE_URL', 'invalid public DNS result') from error
    return urlunsplit(('https', host, parts.path or '/', parts.query, '')), host, addresses[0]


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address, timeout):
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        connection = socket.create_connection((self.address, 443), timeout=self.timeout)
        try:
            require(ipaddress.ip_address(connection.getpeername()[0]) == ipaddress.ip_address(self.address),
                    'UNSAFE_URL', 'connected peer differs from pinned address')
            self.sock = self._context.wrap_socket(connection, server_hostname=self.host)
        except BaseException:
            connection.close()
            raise


def request(url, host, address, max_bytes, method='GET', body=None, headers=None, timeout=15):
    require(os.environ.get('CORPUS_OFFLINE') != '1', 'NETWORK_DISABLED', 'offline profile forbids all network requests')
    connection = PinnedHTTPS(host, address, timeout=timeout)
    try:
        parts = urlsplit(url)
        path = parts.path + ('?' + parts.query if parts.query else '')
        connection.request(method, path, body=body, headers=headers or {'Accept': 'text/plain,text/html,application/pdf'})
        response = connection.getresponse()
        length = response.getheader('Content-Length')
        if length is not None:
            require(length.isdigit() and int(length) <= max_bytes, 'RESPONSE_LIMIT', 'response size exceeds bound')
        content = response.read(max_bytes + 1)
        require(len(content) <= max_bytes, 'RESPONSE_LIMIT', 'response size exceeds bound')
        return response.status, {key.lower(): value for key, value in response.getheaders()}, content
    except (OSError, http.client.HTTPException) as error:
        raise CorpusError('NETWORK_FAILED', 'HTTPS request failed', retryable=True) from error
    finally:
        connection.close()


def fetch_snapshot(url, max_bytes=10 * 1024 * 1024, max_redirects=3, resolver=resolve_public, transport=request):
    require(type(max_bytes) is int and 1 <= max_bytes <= 10 * 1024 * 1024, 'INVALID_BUDGET', 'snapshot size budget invalid')
    require(type(max_redirects) is int and 0 <= max_redirects <= 5, 'INVALID_BUDGET', 'redirect budget invalid')
    for redirect in range(max_redirects + 1):
        normalized, host, address = validate_url(url, resolver)
        status, headers, content = transport(normalized, host, address, max_bytes)
        require(type(content) is bytes and len(content) <= max_bytes, 'RESPONSE_LIMIT', 'response size exceeds bound')
        if status in (301, 302, 303, 307, 308):
            require(redirect < max_redirects and bool(headers.get('location')), 'REDIRECT_LIMIT', 'redirect limit or missing location')
            url = urljoin(normalized, headers['location'])
            continue
        require(status == 200, 'HTTP_FAILED', 'snapshot HTTP status ' + str(status))
        return {'content': content, 'url': normalized, 'media_type': headers.get('content-type', 'application/octet-stream').split(';')[0],
                'redirects': redirect, 'dns_pinned': True}
    raise CorpusError('REDIRECT_LIMIT', 'redirect budget exhausted')
