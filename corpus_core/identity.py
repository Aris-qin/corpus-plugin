import json
import re
from urllib.parse import urlsplit, urlunsplit

from .errors import CorpusError, require


def canonical_owner(db, owner_id):
    seen = set()
    while owner_id not in seen and len(seen) < 32:
        seen.add(owner_id)
        row = db.execute('SELECT metadata FROM works WHERE id=?', (owner_id,)).fetchone()
        require(row is not None, 'IDENTITY_REVIEW_REQUIRED', 'identifier owner missing')
        successor = json.loads(row['metadata']).get('merged_into')
        if not successor:
            return owner_id
        owner_id = successor
    raise CorpusError('IDENTITY_REVIEW_REQUIRED', 'invalid canonical identifier ownership')


def identifier_conflict(db, scheme, lookup_key, owner_id):
    canonical = canonical_owner(db, owner_id)
    owners = db.execute('SELECT DISTINCT owner_id FROM external_identifiers WHERE scheme=? AND lookup_key=?',
                        (scheme, lookup_key))
    return any(canonical_owner(db, row['owner_id']) != canonical for row in owners)


def normalize_identifier(scheme, raw):
    require(type(raw) is str and 0 < len(raw) <= 4096 and raw == raw.strip(), 'INVALID_IDENTIFIER', 'identifier must not contain surrounding whitespace')
    if scheme == 'doi':
        key = re.sub(r'^https://doi\.org/', '', raw, flags=re.I)
        require(re.fullmatch(r'10\.[0-9]{4,9}/[^\s]+', key) is not None, 'INVALID_IDENTIFIER', 'invalid DOI format')
        return key.lower()
    if scheme == 'pmid':
        require(re.fullmatch(r'[1-9][0-9]{0,11}', raw) is not None, 'INVALID_IDENTIFIER', 'invalid PMID')
        return raw
    if scheme == 'pmcid':
        require(re.fullmatch(r'PMC[1-9][0-9]{0,11}', raw) is not None, 'INVALID_IDENTIFIER', 'invalid PMCID')
        return raw
    if scheme == 'url':
        try:
            parts = urlsplit(raw)
            require(parts.scheme in ('http', 'https') and bool(parts.hostname) and not parts.username and not parts.password,
                    'INVALID_IDENTIFIER', 'invalid URL identifier')
            port = parts.port
            host = parts.hostname.encode('idna').decode('ascii').lower()
        except (ValueError, UnicodeError) as error:
            raise CorpusError('INVALID_IDENTIFIER', 'invalid URL identifier') from error
        require(not any(character.isspace() for character in raw), 'INVALID_IDENTIFIER', 'URL contains whitespace')
        if ':' in host:
            host = '[' + host + ']'
        if port is not None:
            host += ':' + str(port)
        return urlunsplit((parts.scheme.lower(), host, parts.path or '/', parts.query, parts.fragment))
    raise CorpusError('INVALID_IDENTIFIER', 'unsupported identifier scheme')
