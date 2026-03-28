import hashlib
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.backends import default_backend
import binascii

def _get_checksum(working_key: str):
    """Derive 128-bit key from working_key using MD5."""
    return hashlib.md5(working_key.encode()).digest()

def encrypt_ccavenue(plain_text: str, working_key: str) -> str:
    """
    Encrypt the request data using AES-128-CBC.
    Used for Web Initialization and Seamless Payment Options fetching.
    """
    key = _get_checksum(working_key)
    # Standard CCAvenue IV (16 bytes of incrementing values or zeros)
    iv = b'\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\x0c\r\x0e\x0f'
    
    padder = padding.PKCS7(128).padder()
    padded_data = padder.update(plain_text.encode()) + padder.finalize()
    
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
    encryptor = cipher.encryptor()
    encrypted_data = encryptor.update(padded_data) + encryptor.finalize()
    
    return binascii.hexlify(encrypted_data).decode()

def decrypt_ccavenue(cipher_text: str, working_key: str) -> str:
    """
    Decrypt the response data from CCAvenue (AES-128-CBC).
    """
    key = _get_checksum(working_key)
    iv = b'\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\x0c\r\x0e\x0f'
    
    encrypted_data = binascii.unhexlify(cipher_text)
    
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
    decryptor = cipher.decryptor()
    decrypted_padded_data = decryptor.update(encrypted_data) + decryptor.finalize()
    
    unpadder = padding.PKCS7(128).unpadder()
    decrypted_data = unpadder.update(decrypted_padded_data) + unpadder.finalize()
    
    return decrypted_data.decode()
