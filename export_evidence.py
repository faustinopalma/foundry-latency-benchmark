import base64
import hashlib
import io
import os
from pathlib import Path
import zipfile
import urllib.request
import json
import re
from datetime import datetime, timezone
from pathlib import PurePosixPath


def build_archive(workspace, run_id, container, snapshot_label=None):
    if snapshot_label is not None and not re.fullmatch(r'[a-z0-9-]{1,40}', snapshot_label):
        raise ValueError('Invalid snapshot label')
    root = workspace / 'results' / run_id
    prefix = f'runs/{run_id}/'
    started_utc = datetime.now(timezone.utc).isoformat()
    remote_names = {blob.name for blob in container.list_blobs(name_starts_with=prefix)}
    inventory_ended_utc = datetime.now(timezone.utc).isoformat()
    if not remote_names or prefix + 'manifest.json' not in remote_names:
        raise RuntimeError('No manifest-backed evidence files to verify')
    if snapshot_label is None:
        expected_names = {prefix + path.relative_to(root).as_posix() for path in root.rglob('*.json')}
        if remote_names != expected_names:
            raise RuntimeError('Cloud and local evidence inventories differ')
        if not (workspace / 'process-status.json').exists():
            raise RuntimeError('Benchmark has not finished; final export refused')
    verified = []
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for remote_name in sorted(remote_names):
            relative = PurePosixPath(remote_name.removeprefix(prefix))
            if not remote_name.startswith(prefix) or relative.is_absolute() or '..' in relative.parts or '\\' in str(relative) or relative.suffix != '.json':
                raise RuntimeError('Invalid evidence path')
            path = root.joinpath(*relative.parts)
            local_bytes = path.read_bytes()
            local = local_bytes.removesuffix(b'\n')
            remote = container.download_blob(remote_name).readall()
            if local != remote:
                raise RuntimeError(f'Cloud evidence mismatch: {remote_name}')
            json.loads(remote)
            archive.writestr(f'results/{run_id}/{relative.as_posix()}', local_bytes)
            verified.append({'name': remote_name, 'sha256': hashlib.sha256(remote).hexdigest(), 'bytes': len(remote)})
        for name in ['benchmark.py', 'run_benchmark.py', 'requirements.txt', 'config.json']:
            archive.write(workspace / name, name)
        for name in ['process-status.json', 'run_network.py', 'run_campaign.py', 'campaign.json']:
            if (workspace / name).exists():
                archive.write(workspace / name, name)
        scope = 'captured_blob_inventory' if snapshot_label is not None else 'final_full_inventory'
        archive.writestr('storage-verification.json', json.dumps({'run_id': run_id, 'all_equal': True, 'inventory_scope': scope, 'files': verified}))
        if snapshot_label is not None:
            archive.writestr('snapshot.json', json.dumps({'run_id': run_id, 'label': snapshot_label, 'preliminary': True,
                              'inventory_started_utc': started_utc, 'inventory_ended_utc': inventory_ended_utc,
                              'verification_ended_utc': datetime.now(timezone.utc).isoformat(), 'verified_records': len(verified),
                              'full_run_inventory_claimed': False, 'selection': 'Fixed inventory returned by Blob listing; concurrent or not-yet-uploaded records can be absent.'}))
    return buffer.getvalue()


def stream_archive(payload):
    print('EVIDENCE_BEGIN ' + hashlib.sha256(payload).hexdigest(), flush=True)
    for index, offset in enumerate(range(0, len(payload), 2048)):
        chunk = payload[offset:offset + 2048]
        encoded = base64.urlsafe_b64encode(chunk).decode('ascii')
        for attempt in range(10):
            print(f'CHUNK {index} {hashlib.sha256(chunk).hexdigest()} {encoded} ENDCHUNK', flush=True)
            acknowledgement = input('ACK>')
            if acknowledgement.strip() == f'NEXT {index}':
                break
        else:
            raise RuntimeError(f'Chunk {index} could not be transferred intact')
    print('EVIDENCE_END', flush=True)


def main():
    from azure.identity import ManagedIdentityCredential
    from azure.storage.blob import BlobServiceClient

    os.chdir('/work')
    snapshot_label = os.environ.get('SNAPSHOT_LABEL')
    if snapshot_label is None and not Path('process-status.json').exists():
        with urllib.request.urlopen('http://127.0.0.1:8765/wait', timeout=7550) as response:
            response.read()
        if not Path('process-status.json').exists():
            raise SystemExit('Benchmark has not finished; export refused')
    config = json.loads(Path('config.json').read_text())
    with ManagedIdentityCredential(client_id=os.environ['AZURE_CLIENT_ID']) as credential:
        with BlobServiceClient(config['storage_endpoint'], credential=credential) as service:
            payload = build_archive(Path.cwd(), os.environ['RUN_ID'], service.get_container_client('benchmark'), snapshot_label)
    stream_archive(payload)


if __name__ == '__main__':
    main()