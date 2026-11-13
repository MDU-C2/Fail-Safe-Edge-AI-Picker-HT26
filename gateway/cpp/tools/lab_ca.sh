#!/usr/bin/env bash
# ================================================================
# lab_ca.sh: the project's private lab CA (see gateway/cpp/TLS.md)
# ================================================================
#
#   gateway/cpp/tools/lab_ca.sh init-ca
#   gateway/cpp/tools/lab_ca.sh issue-server NAME SAN [SAN ...]
#   gateway/cpp/tools/lab_ca.sh issue-client NAME          (mTLS, phase 2)
#   gateway/cpp/tools/lab_ca.sh show [NAME]
#
# SAN is IP:<address> or DNS:<name>, e.g.
#
#   lab_ca.sh issue-server gateway-lynx IP:169.254.219.127 IP:192.168.125.20 IP:127.0.0.1 DNS:localhost
#
# Everything private (the CA key, issued keys) stays in LAB_CA_DIR
# on this machine, outside the repository (default ~/ht26-lab-ca).
# init-ca copies only the public CA certificate into the repository
# (gateway/tls/lab-ca.crt), where clients find it.
#
# Environment:
#   LAB_CA_DIR         CA directory (default ~/ht26-lab-ca)
#   LAB_CA_PASSPHRASE  CA key passphrase; prompted for when unset
#   LAB_CA_PUBLIC      where init-ca copies the public CA certificate
#                      (default gateway/tls/lab-ca.crt in the repository)
#   DAYS               validity of an issued certificate (default 395)
#   CA_DAYS            validity of the CA (default 1095)
#
# Run from Git Bash or WSL; needs OpenSSL 3.4 or newer (-not_before).
#

set -euo pipefail

#
# Git Bash would turn "/CN=..." into a Windows path.
#
export MSYS_NO_PATHCONV=1

CA_DIR="${LAB_CA_DIR:-$HOME/ht26-lab-ca}"
DAYS="${DAYS:-395}"
CA_DAYS="${CA_DAYS:-1095}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
PUBLIC_CA="${LAB_CA_PUBLIC:-$REPO_ROOT/gateway/tls/lab-ca.crt}"

#
# Git Bash's openssl is a Windows program: with path conversion off
# (above) it cannot open /c/Users/..., so give it C:/Users/...
#
if command -v cygpath > /dev/null; then
    CA_DIR="$(cygpath -m "$CA_DIR")"
    PUBLIC_CA="$(cygpath -m "$PUBLIC_CA")"
fi

#
# The only addresses and names any certificate of this CA can be
# valid for, so a stolen CA key cannot impersonate anything else.
#
NAME_CONSTRAINTS="critical"
NAME_CONSTRAINTS+=",permitted;IP:169.254.0.0/255.255.0.0"
NAME_CONSTRAINTS+=",permitted;IP:192.168.125.0/255.255.255.0"
NAME_CONSTRAINTS+=",permitted;IP:127.0.0.0/255.0.0.0"
NAME_CONSTRAINTS+=",permitted;DNS:localhost"
NAME_CONSTRAINTS+=",permitted;DNS:.ht26.lab"

# ================================================================
# Helpers
# ================================================================

die() {
    echo "lab_ca.sh: $*" >&2
    exit 1
}

usage() {
    sed -n '5,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 2
}

pass_args() {
    #
    # -passin/-passout for the CA key: the environment variable if
    # set, else OpenSSL prompts.
    #
    if [[ -n "${LAB_CA_PASSPHRASE:-}" ]]; then
        echo "-$1 env:LAB_CA_PASSPHRASE"
    fi
}

check_openssl() {
    command -v openssl > /dev/null || die "openssl not found"

    #
    # -not_before (backdating) arrived in OpenSSL 3.4.
    #
    local version
    version="$(openssl version | awk '{print $2}')"

    local major minor
    major="${version%%.*}"
    minor="${version#*.}"
    minor="${minor%%.*}"

    if (( major < 3 || (major == 3 && minor < 4) )); then
        die "OpenSSL $version is too old; 3.4 or newer is needed (Git Bash ships one)"
    fi
}

check_name() {
    [[ "$1" =~ ^[a-z0-9][a-z0-9-]*$ ]] || die "NAME must be lowercase letters, digits and dashes: $1"
}

#
# Three days back, so a device whose clock is a little behind still
# accepts a fresh certificate.
#
not_before() {
    date -u -d '-3 days' +%Y%m%d%H%M%SZ
}

require_ca() {
    [[ -f "$CA_DIR/lab-ca.key" && -f "$CA_DIR/lab-ca.crt" ]] \
        || die "no CA in $CA_DIR; run 'lab_ca.sh init-ca' first (or set LAB_CA_DIR)"
}

describe() {
    openssl x509 -in "$1" -noout \
        -subject -issuer -startdate -enddate \
        -ext subjectAltName,extendedKeyUsage,nameConstraints 2> /dev/null \
        | sed 's/^/  /'

    echo "  SHA256 fingerprint: $(fingerprint "$1")"
}

fingerprint() {
    openssl x509 -in "$1" -noout -fingerprint -sha256 \
        | sed 's/^.*=//; s/://g' \
        | tr 'A-F' 'a-f'
}

# ================================================================
# Issuing
# ================================================================

#
# Key, request and certificate for NAME under $CA_DIR/issued/NAME/.
# $2 is the extendedKeyUsage, the rest the SANs.
#
issue() {
    local name="$1"
    local usage="$2"
    shift 2

    check_name "$name"
    require_ca

    local out="$CA_DIR/issued/$name"

    mkdir -p "$out"

    if [[ -f "$out/$name.crt" ]]; then
        #
        # Renewal: keep the old pair next to the new one.
        #
        local stamp
        stamp="$(date -u +%Y%m%d-%H%M%S)"

        mkdir -p "$out/old-$stamp"
        mv "$out/$name".* "$out/old-$stamp/"

        echo "Moved the previous key and certificate to $out/old-$stamp/"
    fi

    local extensions="$out/$name.ext"

    {
        echo "basicConstraints=critical,CA:FALSE"
        echo "keyUsage=critical,digitalSignature"
        echo "extendedKeyUsage=$usage"
        echo "subjectKeyIdentifier=hash"
        echo "authorityKeyIdentifier=keyid:always"

        if (( $# > 0 )); then
            local sans
            sans="$(printf '%s,' "$@")"
            echo "subjectAltName=${sans%,}"
        fi
    } > "$extensions"

    #
    # A new key every time: renewing never reuses a key.
    #
    (
        umask 077
        openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out "$out/$name.key"
    )

    openssl req -new \
        -key "$out/$name.key" \
        -subj "/O=MDU Robotics HT26/CN=$name" \
        -out "$out/$name.csr"

    # shellcheck disable=SC2046
    openssl x509 -req \
        -in "$out/$name.csr" \
        -CA "$CA_DIR/lab-ca.crt" \
        -CAkey "$CA_DIR/lab-ca.key" \
        $(pass_args passin) \
        -CAserial "$CA_DIR/lab-ca.srl" \
        -CAcreateserial \
        -sha256 \
        -not_before "$(not_before)" \
        -days "$DAYS" \
        -extfile "$extensions" \
        -out "$out/$name.crt"

    if ! openssl verify -CAfile "$CA_DIR/lab-ca.crt" "$out/$name.crt" > /dev/null; then
        #
        # E.g. a SAN outside the CA's name constraints.
        #
        rm -f "$out/$name.crt"
        die "the new certificate does not verify against the CA (a SAN outside the CA's permitted addresses?)"
    fi

    echo
    echo "Issued $name:"
    describe "$out/$name.crt"
    echo
    echo "  key:         $out/$name.key   (private: never into git or the image)"
    echo "  certificate: $out/$name.crt"
}

# ================================================================
# Commands
# ================================================================

cmd_init_ca() {
    if [[ -e "$CA_DIR/lab-ca.key" ]]; then
        die "$CA_DIR already holds a CA; delete it first to start over (every client then needs the new lab-ca.crt)"
    fi

    mkdir -p "$CA_DIR/issued"

    echo "Creating the lab CA in $CA_DIR"
    echo "Choose a passphrase for the CA key and keep it with the key's backup."

    # shellcheck disable=SC2046
    (
        umask 077
        openssl genpkey \
            -algorithm EC \
            -pkeyopt ec_paramgen_curve:P-256 \
            -aes256 \
            $(pass_args pass) \
            -out "$CA_DIR/lab-ca.key"
    )

    # shellcheck disable=SC2046
    openssl req -x509 -new \
        -key "$CA_DIR/lab-ca.key" \
        $(pass_args passin) \
        -sha256 \
        -not_before "$(not_before)" \
        -days "$CA_DAYS" \
        -subj "/O=MDU Robotics HT26/CN=HT26 Lab CA" \
        -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
        -addext "keyUsage=critical,keyCertSign,cRLSign" \
        -addext "subjectKeyIdentifier=hash" \
        -addext "nameConstraints=$NAME_CONSTRAINTS" \
        -out "$CA_DIR/lab-ca.crt"

    mkdir -p "$(dirname "$PUBLIC_CA")"
    cp "$CA_DIR/lab-ca.crt" "$PUBLIC_CA"

    echo
    echo "CA created:"
    describe "$CA_DIR/lab-ca.crt"
    echo
    echo "  key:         $CA_DIR/lab-ca.key   (encrypted; back it up offline, never into git)"
    echo "  certificate: $CA_DIR/lab-ca.crt"
    echo "  copied to:   $PUBLIC_CA   (public; commit it, clients trust it)"
}

cmd_show() {
    require_ca

    if (( $# == 0 )); then
        echo "CA ($CA_DIR):"
        describe "$CA_DIR/lab-ca.crt"

        local certificate
        for certificate in "$CA_DIR"/issued/*/*.crt; do
            [[ -f "$certificate" ]] || continue

            echo
            echo "$(basename "$certificate" .crt):"
            describe "$certificate"
        done

        return
    fi

    check_name "$1"

    local certificate="$CA_DIR/issued/$1/$1.crt"

    [[ -f "$certificate" ]] || die "nothing issued as $1"

    describe "$certificate"
}

main() {
    (( $# > 0 )) || usage

    check_openssl

    local command="$1"
    shift

    case "$command" in
        init-ca)
            cmd_init_ca
            ;;
        issue-server)
            (( $# >= 2 )) || die "usage: issue-server NAME SAN [SAN ...]  (SAN = IP:<address> or DNS:<name>)"
            local name="$1"
            shift
            local san
            for san in "$@"; do
                [[ "$san" =~ ^(IP|DNS):[^,[:space:]]+$ ]] || die "bad SAN '$san' (IP:<address> or DNS:<name>)"
            done
            issue "$name" serverAuth "$@"
            ;;
        issue-client)
            (( $# == 1 )) || die "usage: issue-client NAME"
            issue "$1" clientAuth
            ;;
        show)
            cmd_show "$@"
            ;;
        -h | --help | help)
            usage
            ;;
        *)
            die "unknown command '$command'"
            ;;
    esac
}

main "$@"
