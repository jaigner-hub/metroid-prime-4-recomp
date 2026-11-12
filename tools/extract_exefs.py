#!/usr/bin/env python3
"""Read an XCI or NSP in place; extract and verify its base or explicitly selected update Program ExeFS.

Supports NCA3, key-area or common-ticket AES-CTR, and hierarchical SHA-256 PFS0.
Keys stay in memory. No RomFS extraction, executable launch, or network calls.

Copied unchanged from fe3h-pc-recomp tools/extract_exefs.py @ 47ef99126793dbd13e4a475feb910a13ef6639b8 (MIT, Copyright (c) 2026
Jeff Aigner), apart from this header and one MP4 change: container_entries() clips the XCI root table's entries to
the file, so a trimmed XCI opens (issue #11). That file is adapted from MacNativeGames games/totk/scripts/extract_exefs.py @ b75be8cfa704df3701e86130c8ced268e79d6b11 (MIT,
Copyright (c) 2026 MacNativeGames contributors), as vendored unchanged by totk-pc-recomp tools/vendor/macnativegames/
@ c944f9f94520874f2a669add30dd08d29ee9f7b5. Changes: program_nca() takes the title id from the package instead of
TotK's (optionally checked with --title-id); the PFS0 section reader is a function, read_pfs0_section(), so the Meta
NCA's CNMT can be read too; the manifest and the output report the title id and version from the CNMT.
"""
import argparse
import hashlib
import json
import struct
import io
from pathlib import Path
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


def require(condition, message):
    if not condition:
        raise ValueError(message)


def decrypt(key, mode, data):
    ctx = Cipher(algorithms.AES(key), mode).decryptor()
    return ctx.update(data) + ctx.finalize()


def read_at(f, offset, length):
    require(0 <= offset and 0 <= length <= 512 * 1024 * 1024, 'Invalid/big read')
    f.seek(offset)
    data = f.read(length)
    require(len(data) == length, 'Truncated input')
    return data


def hfs(f, offset, bound, clip=False):
    return partition_entries(f, offset, bound, b'HFS0', 64, clip)


def partition_entries(f, offset, bound, magic=b'PFS0', stride=24, clip=False):
    """Read a bounded PFS0/HFS0 table without loading its file payloads. clip: an entry that starts inside the bound but
    ends past it is cut at the bound instead of rejected (the XCI root table of a trimmed dump, see container_entries)."""
    require(bound >= 16, 'Truncated partition header')
    hdr = read_at(f, offset, 16)
    require(hdr[:4] == magic, f'Expected {magic.decode()}')
    count, strings_size = struct.unpack_from('<II', hdr, 4)
    require(count <= 4096 and strings_size <= 1024 * 1024, 'Invalid partition table')
    size = 16 + count * stride + strings_size
    require(size <= bound, 'Partition table out of bounds')
    raw = read_at(f, offset, size)
    strings = raw[16 + count * stride:]
    entries = []
    names = set()
    for i in range(count):
        off, length, nameoff = struct.unpack_from('<QQI', raw, 16 + i * stride)
        require(nameoff < len(strings), 'Invalid partition filename offset')
        end = strings.find(b'\0', nameoff)
        require(end >= 0, 'Unterminated partition filename')
        name = strings[nameoff:end].decode('utf-8')
        require(name not in ('', '.', '..') and '/' not in name and '\\' not in name, 'Unsafe partition filename')
        require(name not in names, 'Duplicate partition filename')
        names.add(name)
        if clip and size + off <= bound:
            length = min(length, bound - size - off)
        require(size + off + length <= bound, 'Partition entry out of bounds')
        entries.append((name, offset + size + off, length))
    end = offset + size
    for _, start, length in sorted(entries, key=lambda e: e[1]):
        require(start >= end, 'Overlapping partition entries')
        end = start + length
    return entries


def container_entries(f, size):
    if read_at(f, 0, 4) == b'PFS0':
        return 'nsp', partition_entries(f, 0, size)
    require(size >= 0x138 and read_at(f, 0x100, 4) == b'HEAD', 'Expected an XCI or NSP (PFS0) game file')
    root = struct.unpack('<Q', read_at(f, 0x130, 8))[0]
    # A trimmed XCI (the common dump format) ends at the last file of the secure partition, while the root table still
    # counts the partition's trailing padding (512 bytes in this game's cartridge image). Clip the root entries to the
    # file; the secure partition's own entries are then checked strictly, so a file that is really cut short still fails.
    partitions = hfs(f, root, size - root, clip=True)
    secure = [e for e in partitions if e[0] == 'secure']
    require(len(secure) == 1, 'Expected one XCI secure partition')
    return 'xci', hfs(f, secure[0][1], secure[0][2])


def load_keys(path):
    keys = {}
    for number, line in enumerate(path.read_text().splitlines(), 1):
        line = line.split('#', 1)[0].strip()
        if '=' in line:
            name, value = line.split('=', 1)
            try:
                keys[name.strip().lower()] = bytes.fromhex(value.strip())
            except ValueError:
                raise ValueError(f'Invalid key encoding at line {number}') from None
    return keys


def common_tickets(f, entries):
    """Keep encrypted title keys in memory only, as nxnative's NSP importer does.

    Ticket offsets below are relative to the signed body; accept the standard RSA/ECDSA layouts.
    A personalized ticket cannot provide a common title key and is never treated as one.
    """
    tickets = {}
    bodies = {0x10000: 0x240, 0x10001: 0x140, 0x10002: 0x80,
              0x10003: 0x240, 0x10004: 0x140, 0x10005: 0x80}
    for name, offset, size in entries:
        if not name.lower().endswith('.tik'):
            continue
        require(size >= 4, 'Truncated NSP ticket')
        # Switch ticket signature type is little-endian: RSA2048/SHA256 = 04 00 01 00.
        sig = struct.unpack('<I', read_at(f, offset, 4))[0]
        require(sig in bodies, 'Unsupported NSP ticket signature type')
        body = bodies[sig]
        require(size >= body + 0x180, 'Truncated NSP ticket body')
        data = read_at(f, offset + body, 0x180)
        require(data[0x141] == 0, 'Personalized NSP tickets are unsupported; a common ticket is required')
        rights, key = data[0x160:0x170], data[0x40:0x50]
        require(rights not in tickets or tickets[rights] == key, 'Conflicting NSP tickets')
        tickets[rights] = key
    return tickets


def read_nca_header(f, offset, size, keys):
    require(size >= 0xc00, 'Truncated NCA header')
    require('header_key' in keys, 'Missing header_key in prod.keys')
    raw = read_at(f, offset, 0xc00)
    hdr = b''.join(decrypt(keys['header_key'], modes.XTS(i.to_bytes(16, 'big')),
                           raw[i * 512:(i + 1) * 512]) for i in range(6))
    require(hdr[0x200:0x204] == b'NCA3', 'Expected NCA3 (check prod.keys and the game file)')
    require(struct.unpack_from('<Q', hdr, 0x208)[0] == size, 'NCA size does not match container entry')
    for i in range(4):
        start, end = struct.unpack_from('<II', hdr, 0x240 + i * 16)
        require((start == end == 0) or 0xc00 <= start * 512 < end * 512 <= size, 'NCA section out of bounds')
    return hdr


def nca_ctr_key(hdr, keys, tickets):
    generation = max(max(hdr[0x206], hdr[0x220]) - 1, 0)
    rights = hdr[0x230:0x240]
    if rights != bytes(16):
        require(rights in tickets, 'Missing common ticket for rights-managed NCA in the NSP')
        keyname = f'titlekek_{generation:02x}'
        require(keyname in keys, f'Missing {keyname} in prod.keys')
        return decrypt(keys[keyname], modes.ECB(), tickets[rights])
    require(hdr[0x207] < 3, 'Unsupported NCA key area')
    area = ('application', 'ocean', 'system')[hdr[0x207]]
    keyname = f'key_area_key_{area}_{generation:02x}'
    require(keyname in keys, f'Missing {keyname} in prod.keys')
    return decrypt(keys[keyname], modes.ECB(), hdr[0x300:0x340])[32:48]


def nca_inventory(f, entries, keys):
    records = []
    for name, offset, length in entries:
        if not name.lower().endswith('.nca'):
            continue
        hdr = read_nca_header(f, offset, length, keys)
        record = dict(name=name, nca_offset=offset, size=length, content_type=hdr[0x205],
                      title_id=f'{struct.unpack_from("<Q", hdr, 0x210)[0]:016X}')
        records.append((record, hdr))
    return records


def program_nca(records, update=False, title_id=None):
    """(record, header) of the package's one Program NCA. A base package's Meta NCA carries the program's title id; an
    update's carries that id with 0x800 set and its Program NCA keeps the base id. title_id: the expected base id."""
    programs = [(r, h) for r, h in records if r['content_type'] == 0]
    require(len(programs) == 1, f'Expected one Program NCA, found {len(programs)}; combined packages are unsupported')
    record, hdr = programs[0]
    base_id = int(record['title_id'], 16)
    require(base_id & 0xFFF == 0, f'Program title id {record["title_id"]} is not a base application id')
    if title_id is not None:
        require(base_id == int(title_id, 16), f'Package belongs to title {record["title_id"]}, not {title_id.upper()}')
    meta_id = f'{base_id | 0x800:016X}' if update else record['title_id']
    metadata = [r for r, _ in records if r['content_type'] == 1]
    require(len(metadata) == 1 and metadata[0]['title_id'] == meta_id,
            f'Expected one {"update" if update else "base game"} Meta NCA for {meta_id}; '
            'updates, DLC and combined packages are unsupported here')
    bktr = any(hdr[0x400 + i * 512 + 4] == 4 for i in range(4))
    require(bktr == update, 'Expected a BKTR update, not a base game' if update else 'Expected a base game, not an update')
    require(all(r['title_id'] in (record['title_id'], meta_id) for r, _ in records), 'Mixed-title package')
    return record, hdr


def read_pfs0_section(f, record, hdr, ctr_key, i):
    """Files [(name, bytes)] of NCA section i, a hierarchical SHA-256 PFS0, with every hash level verified; None when
    section i is empty or another kind."""
    start, end = struct.unpack_from('<II', hdr, 0x240 + 16 * i)
    if not start:
        return None
    fs = hdr[0x400 + 512 * i:0x600 + 512 * i]
    require(hashlib.sha256(fs).digest() == hdr[0x280 + 32 * i:0x2A0 + 32 * i], 'FS header hash mismatch')
    if fs[2:4] != b'\x01\x02':
        return None
    require(fs[4] in (1, 3), 'Only plaintext or AES-CTR PFS0 sections are supported')
    blocksize, levels = struct.unpack_from('<II', fs, 0x28)
    hashoff, hashsize, pfsoff, pfssize = struct.unpack_from('<QQQQ', fs, 0x30)
    secstart, secsize = start * 512, (end - start) * 512
    require(levels == 2 and blocksize > 0 and pfssize <= 512 * 1024 * 1024, 'Unsupported PFS0 hash layout')

    def section_read(relative, size):
        require(relative >= 0 and relative + size <= secsize, 'Section read out of bounds')
        absolute = secstart + relative
        if fs[4] == 1:
            return read_at(f, record_offset(record) + absolute, size)
        aligned = absolute & ~15
        iv = fs[0x140:0x148][::-1] + (aligned // 16).to_bytes(8, 'big')
        data = read_at(f, record_offset(record) + aligned, size + absolute - aligned)
        return decrypt(ctr_key, modes.CTR(iv), data)[absolute - aligned:]

    hashes = section_read(hashoff, hashsize)
    require(hashlib.sha256(hashes).digest() == fs[8:40], 'PFS0 hash-table mismatch')
    pfs = section_read(pfsoff, pfssize)
    for index, pos in enumerate(range(0, pfssize, blocksize)):
        expected = hashes[index * 32:(index + 1) * 32]
        require(hashlib.sha256(pfs[pos:pos + blocksize]).digest() == expected, f'PFS0 block hash mismatch: {index}')
    return [(name, pfs[off:off + size]) for name, off, size in partition_entries(io.BytesIO(pfs), 0, len(pfs))]


def content_meta(f, records, keys, tickets):
    """(title id, version) from the CNMT in the package's Meta NCA."""
    record, hdr = next((r, h) for r, h in records if r['content_type'] == 1)
    files = read_pfs0_section(f, record, hdr, nca_ctr_key(hdr, keys, tickets), 0)
    cnmt = [data for name, data in files or [] if name.endswith('.cnmt')]
    require(len(cnmt) == 1 and len(cnmt[0]) >= 12, 'Meta NCA holds no CNMT')
    title, version = struct.unpack_from('<QI', cnmt[0], 0)
    return f'{title:016X}', version


def record_offset(record):
    # Read manifests produced before NSP support too.
    return record['nca_offset'] if 'nca_offset' in record else record['xci_offset']


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('game', type=Path, help='base game (.xci or .nsp), or an update .nsp with --update')
    parser.add_argument('--keys', required=True, type=Path)
    parser.add_argument('--update', action='store_true', help='extract update code; caller must verify the version')
    parser.add_argument('--title-id', help='expected base title id (default: accept the package\'s own)')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    require(not args.output.exists(), 'Output already exists; choose a new directory')
    keys = load_keys(args.keys)
    with args.game.open('rb') as f:
        kind, entries = container_entries(f, args.game.stat().st_size)
        tickets = common_tickets(f, entries)
        records = nca_inventory(f, entries, keys)
        record, hdr = program_nca(records, update=args.update, title_id=args.title_id)
        meta_title, version = content_meta(f, records, keys, tickets)
        ctr_key = nca_ctr_key(hdr, keys, tickets)
        for i in range(4):
            files = read_pfs0_section(f, record, hdr, ctr_key, i)
            if files is None:
                continue
            require(any(name == 'main.npdm' for name, _ in files), 'PFS0 is not ExeFS')
            args.output.mkdir(parents=True)
            extracted = []
            for name, data in files:
                (args.output / name).write_bytes(data)
                extracted.append({'name': name, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
            manifest = {'source': str(args.game.resolve()), 'source_size': args.game.stat().st_size,
                        'source_mtime_ns': args.game.stat().st_mtime_ns, 'container': kind,
                        'title_id': record['title_id'], 'meta_title_id': meta_title, 'version': version,
                        'nca_inventory': [r for r, _ in records], 'program': record, 'section_index': i,
                        'integrity': 'FS header, ExeFS hash table, and every ExeFS data block verified; RSA signatures not checked',
                        'files': extracted}
            (args.output.parent / 'extraction-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
            print(json.dumps({'title_id': record['title_id'], 'version': version, 'integrity': manifest['integrity'],
                              'files': extracted}, indent=2))
            return
        raise ValueError('No ExeFS section found')


if __name__ == '__main__':
    main()
