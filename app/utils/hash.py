import hashlib


def compute_hash(data: bytes, algorithm: str = "sha256") -> str:
    h = hashlib.new(algorithm)
    h.update(data)
    return h.hexdigest()


def compute_file_hash(filepath: str, algorithm: str = "sha256", chunk_size: int = 8192) -> str:
    h = hashlib.new(algorithm)
    with open(filepath, "rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


def compute_chunk_hash(chunk_data: bytes) -> str:
    return hashlib.md5(chunk_data).hexdigest()
