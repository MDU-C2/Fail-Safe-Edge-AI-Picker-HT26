import socket
import random
import time

def send_target(x, y, z, host="127.0.0.1", port=1025):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect((host, port))
    s.sendall(f"{x},{y},{z}".encode())
    response = s.recv(1024)
    print(f"Sent: ({x}, {y}, {z}) -> Robot says: {response.decode()}")
    s.close()

# Roughly around your known-working range, with some spread
X_RANGE = (-50, 350)
Y_RANGE = (-400, -150)
Z_FIXED = 199.40   # keep Z constant for now, same as your working example

while True:
    x = round(random.uniform(*X_RANGE), 2)
    y = round(random.uniform(*Y_RANGE), 2)

    try:
        send_target(x, y, Z_FIXED)
    except (ConnectionRefusedError, ConnectionResetError, TimeoutError) as e:
        print(f"Connection failed: {e}")

    time.sleep(2)