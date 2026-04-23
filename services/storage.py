import os
import io
import logging
import paramiko
import uuid
import httpx
from core.config import settings

logger = logging.getLogger("GamerzAdda.storage")

def upload_file(data: bytes, filename: str, sub_dir: str = "general") -> str:
    """
    Uploads a file. 
    Priority: VPS API (HTTP) > VPS SFTP (SSH) > Local Static (fallback).
    Returns the public URL of the uploaded file.
    """
    # 1. Try VPS API (Best for Firewalls)
    if settings.VPS_API_UPLOAD_URL:
        try:
            return _upload_via_api(data, filename, sub_dir)
        except Exception as e:
            logger.error(f"!!! VPS API UPLOAD ERROR: {type(e).__name__}: {str(e)}")

    # 2. Try VPS SFTP (SSH)
    if settings.VPS_STORAGE_ENABLED and settings.VPS_HOST:
        if not settings.VPS_USERNAME:
            logger.error("VPS storage enabled but VPS_USERNAME is missing.")
        else:
            try:
                return _upload_to_vps(data, filename, sub_dir)
            except Exception as e:
                logger.error(f"!!! VPS SFTP UPLOAD ERROR: {type(e).__name__}: {str(e)}")
                logger.warning("Falling back to local storage due to VPS error.")
    
    # 3. Fallback to Local
    return _upload_to_local(data, filename, sub_dir)

def _upload_via_api(data: bytes, filename: str, sub_dir: str) -> str:
    """Uploads file to VPS via HTTP API script."""
    logger.info(f"Attempting VPS API upload: {filename} to {settings.VPS_API_UPLOAD_URL}")
    
    with httpx.Client(timeout=15.0) as client:
        files = {"file": (filename, data, "image/jpeg")}
        payload = {
            "secret": settings.VPS_API_SECRET,
            "sub_dir": sub_dir
        }
        
        response = client.post(settings.VPS_API_UPLOAD_URL, data=payload, files=files)
        
        if response.status_code == 200 and response.text.strip() == "SUCCESS":
            logger.info(f"File {filename} successfully uploaded via VPS API")
            
            public_base = settings.VPS_PUBLIC_BASE_URL.rstrip("/")
            if not public_base:
                # Guess from API URL
                public_base = settings.VPS_API_UPLOAD_URL.rsplit("/", 1)[0]
                
            return f"{public_base}/static/{sub_dir}/{filename}"
        else:
            raise Exception(f"API returned {response.status_code}: {response.text}")

def _upload_to_vps(data: bytes, filename: str, sub_dir: str) -> str:
    """Uploads file to VPS via SFTP using SSHClient for better stability."""
    logger.info(f"Attempting VPS SFTP upload: {filename} to {settings.VPS_HOST}:{settings.VPS_PORT}")
    
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    try:
        # Load credentials
        pkey = None
        if settings.VPS_PRIVATE_KEY:
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
            timeout=10,
            banner_timeout=10,
            auth_timeout=10
        )
        
        sftp = ssh.open_sftp()
        
        # Ensure remote sub_dir exists
        remote_base = settings.VPS_REMOTE_PATH.rstrip("/")
        remote_target_dir = f"{remote_base}/{sub_dir}"
        
        try:
            sftp.mkdir(remote_target_dir)
        except IOError:
            pass 

        remote_file_path = f"{remote_target_dir}/{filename}"
        
        with sftp.open(remote_file_path, "wb") as remote_file:
            remote_file.write(data)
        
        logger.info(f"File {filename} successfully uploaded to VPS via SFTP")
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
