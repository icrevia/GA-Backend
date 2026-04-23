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
    if settings.VPS_STORAGE_ENABLED and settings.VPS_HOST:
        try:
            return _upload_to_vps(data, filename, sub_dir)
        except Exception as e:
            logger.error(f"!!! VPS UPLOAD ERROR: {type(e).__name__}: {str(e)}")
            logger.warning("Falling back to local storage due to VPS error.")
    else:
        if settings.VPS_STORAGE_ENABLED:
            logger.warning("VPS storage enabled but VPS_HOST is missing.")
    
    return _upload_to_local(data, filename, sub_dir)

def _upload_to_vps(data: bytes, filename: str, sub_dir: str) -> str:
    """Uploads file to VPS via SFTP using SSHClient for better stability."""
    logger.info(f"Attempting VPS upload: {filename} to {settings.VPS_HOST}:{settings.VPS_PORT}")
    
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    try:
        # Load credentials
        pkey = None
        if settings.VPS_PRIVATE_KEY:
            logger.info("Using Private Key for VPS auth")
            key_file = io.StringIO(settings.VPS_PRIVATE_KEY)
            try:
                pkey = paramiko.RSAKey.from_private_key(key_file)
            except:
                key_file.seek(0)
                pkey = paramiko.Ed25519Key.from_private_key(key_file)

        # Connect with explicit timeout
        ssh.connect(
            hostname=settings.VPS_HOST,
            port=settings.VPS_PORT,
            username=settings.VPS_USERNAME,
            password=settings.VPS_PASSWORD or None,
            pkey=pkey,
            timeout=10,        # Connection timeout
            banner_timeout=10, # SSH banner timeout
            auth_timeout=10    # Auth timeout
        )
        
        logger.info(f"SSH Connection established to {settings.VPS_HOST}")
        
        sftp = ssh.open_sftp()
        
        # Ensure remote sub_dir exists
        remote_base = settings.VPS_REMOTE_PATH.rstrip("/")
        remote_target_dir = f"{remote_base}/{sub_dir}"
        
        try:
            sftp.mkdir(remote_target_dir)
        except IOError:
            pass # Already exists or parent missing (assume base exists)

        remote_file_path = f"{remote_target_dir}/{filename}"
        
        # Upload data
        with sftp.open(remote_file_path, "wb") as remote_file:
            remote_file.write(data)
        
        logger.info(f"File {filename} successfully uploaded to VPS")
        
        sftp.close()
        
        public_base = settings.VPS_PUBLIC_BASE_URL.rstrip("/")
        if not public_base:
            public_base = f"http://{settings.VPS_HOST}"

        return f"{public_base}/{sub_dir}/{filename}"

    finally:
        ssh.close()

def _upload_to_local(data: bytes, filename: str, sub_dir: str) -> str:
    """Fallback: saves to local static directory (ephemeral on Railway)."""
    logger.info(f"Saving to local fallback storage: {filename}")
    local_dir = os.path.join("static", sub_dir)
    os.makedirs(local_dir, exist_ok=True)
    
    save_path = os.path.join(local_dir, filename)
    with open(save_path, "wb") as f:
        f.write(data)
    
    base_url = (settings.APP_URL or "").rstrip("/")
    return f"{base_url}/static/{sub_dir}/{filename}"
