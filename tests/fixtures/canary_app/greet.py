def greeting(name: str) -> str:
    name = " ".join(name.split())[:64] or "there"
    return f"hello, {name}"
