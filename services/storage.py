import os
import io
import logging
import paramiko
import uuid
from core.config import settings

logger = logging.getLogger("GamerzAdda.storage")

def upload_file(data: bytes, filename: str, sub_dir: str = "general") -> str:
    """
    Uploads a file. 
    Priority: VPS (if enabled) > Local Static (fallback).
    Returns the public URL of the uploaded file.
    """
    if settings.VPS_STORAGE_ENABLED:
        try:
            return _upload_to_vps(data, filename, sub_dir)
        except Exception as e:
            logger.error(f"VPS Upload failed, falling back to local: {e}")
    
    return _upload_to_local(data, filename, sub_dir)

def _upload_to_vps(data: bytes, filename: str, sub_dir: str) -> str:
    """Uploads file to VPS via SFTP."""
    transport = paramiko.Transport((settings.VPS_HOST, settings.VPS_PORT))
    
    if settings.VPS_PRIVATE_KEY:
        # Load private key from string
        key_file = io.StringIO(settings.VPS_PRIVATE_KEY)
        # Try different key types
        try:
            pkey = paramiko.RSAKey.from_private_key(key_file)
        except:
            key_file.seek(0)
            pkey = paramiko.Ed25519Key.from_private_key(key_file)
        transport.connect(username=settings.VPS_USERNAME, pkey=pkey)
    else:
        transport.connect(username=settings.VPS_USERNAME, password=settings.VPS_PASSWORD)

    sftp = paramiko.SFTPClient.from_transport(transport)
    
    # Ensure remote sub_dir exists
    remote_base = settings.VPS_REMOTE_PATH.rstrip("/")
    remote_target_dir = f"{remote_base}/{sub_dir}"
    
    try:
        sftp.mkdir(remote_target_dir)
    except IOError:
        pass # Already exists or parent missing (assume base exists)

    remote_file_path = f"{remote_target_dir}/{filename}"
    
    with sftp.open(remote_file_path, "wb") as remote_file:
        remote_file.write(data)
    
    sftp.close()
    transport.close()

    public_base = settings.VPS_PUBLIC_BASE_URL.rstrip("/")
    if not public_base:
        # Fallback to VPS IP if no domain
        public_base = f"http://{settings.VPS_HOST}"

    return f"{public_base}/{sub_dir}/{filename}"

def _upload_to_local(data: bytes, filename: str, sub_dir: str) -> str:
    """Fallback: saves to local static directory (ephemeral on Railway)."""
    local_dir = os.path.join("static", sub_dir)
    os.makedirs(local_dir, exist_ok=True)
    
    save_path = os.path.join(local_dir, filename)
    with open(save_path, "wb") as f:
        f.write(data)
    
    base_url = (settings.APP_URL or "").rstrip("/")
    return f"{base_url}/static/{sub_dir}/{filename}"
