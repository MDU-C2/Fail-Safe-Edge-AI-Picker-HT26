import hashlib
import secrets


token = secrets.token_urlsafe(32)

token_hash = hashlib.sha256(
    token.encode()
).hexdigest()


print("Token:")
print(token)

print()

print("Token hash:")
print(token_hash)