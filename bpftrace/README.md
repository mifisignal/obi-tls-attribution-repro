# Optional: direct evidence, without OBI

`ssl_bio_binding.bt` demonstrates the underlying binding failure with nothing but
`bpftrace`. It is deliberately separate from the main reproduction: the compose
stack in the parent directory is the primary artifact and needs none of this.

## What it measures

OBI binds an `SSL*` to a socket only if a socket syscall fires while execution is
still inside the `SSL_write` uprobe window. This script counts exactly that, per
`SSL_write`/`SSL_write_ex` call, and prints the distribution at exit as
`@syscalls_inside_ssl_write`.

- **0** means the window closed empty and no binding was possible. The
  instrumentation must fall back to a guess.
- **1 or more** means the ciphertext went to the socket from inside `SSL_write`
  and the binding is sound.

It also prints `SSL_set_bio` (showing the `rbio`/`wbio` heap pointers a memory
BIO is wired to), `BIO_write` (ciphertext movement — note this symbol lives in
**libcrypto**, not libssl), and `sys_enter_sendto`, each tagged with whether it
occurred inside an `SSL_write` window.

## Results observed

Against `app` at `CONCURRENCY=150`, 8-second samples:

```
CLIENT_MODE=asyncio   (memory BIO)     CLIENT_MODE=blocking  (socket BIO)
@syscalls_inside_ssl_write:            @syscalls_inside_ssl_write:
[0]   4903 |@@@@@@@@@@@@@@@@@@@@@|     [0]     35 |                     |
                                       [1]   7045 |@@@@@@@@@@@@@@@@@@@@@|
```

Every single memory-BIO `SSL_write_ex` window contained zero socket syscalls.
Individual lines read:

```
SSL_write_ex>    ssl=0xc4c84e630380 len=117
SSL_write_ex<    ret=1 socket_syscalls_inside_window=0
...
sys_sendto       fd=290 len=1524 INSIDE_SSL_WRITE=NO  <-- unbindable
```

## Running it

Needs a Linux host (or VM), root, `bpftrace`, and the target's libssl/libcrypto
paths. The script uses the placeholders `LIBSSL` and `LIBCRYPTO`, which you
substitute for real paths, and takes the target PID as its first positional
argument.

Bring the stack up first (`docker compose up --build -d` in the parent
directory), then:

```sh
PID=$(docker inspect obi-repro-app --format '{{.State.Pid}}')
ARCH=$(uname -m)-linux-gnu
R=/proc/$PID/root

sed -e "s#LIBSSL#$R/usr/lib/$ARCH/libssl.so.3#g" \
    -e "s#LIBCRYPTO#$R/usr/lib/$ARCH/libcrypto.so.3#g" \
    ssl_bio_binding.bt > /tmp/run.bt

sudo timeout 10 bpftrace /tmp/run.bt "$PID"
```

Referencing the libraries through `/proc/<pid>/root/...` is what lets a
host-side `bpftrace` attach uprobes to a library inside the container's mount
namespace.

## On macOS

`bpftrace` has to run inside Docker Desktop's Linux VM, in a container with
`--privileged --pid=host`. The published `quay.io/iovisor/bpftrace` image is
amd64-only and fails under emulation with `failed to create map: Function not
implemented`. Installing `bpftrace` in a native arm64 Ubuntu container works —
that is how the numbers above were produced:

```sh
PID=$(docker inspect obi-repro-app --format '{{.State.Pid}}')
R=/proc/$PID/root
sed -e "s#LIBSSL#$R/usr/lib/aarch64-linux-gnu/libssl.so.3#g" \
    -e "s#LIBCRYPTO#$R/usr/lib/aarch64-linux-gnu/libcrypto.so.3#g" \
    ssl_bio_binding.bt > /tmp/run.bt

docker run --rm -i --privileged --pid=host \
  -v /sys:/sys -v /lib/modules:/lib/modules:ro ubuntu:24.04 \
  bash -c 'apt-get update -qq && apt-get install -y -qq bpftrace &&
           cat > /tmp/run.bt && timeout 10 bpftrace /tmp/run.bt '"$PID" < /tmp/run.bt
```

Piping the script over stdin avoids relying on a bind mount, since the host path
may not be in Docker Desktop's shared-file set.
