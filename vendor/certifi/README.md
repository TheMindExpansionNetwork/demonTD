# certifi (vendored)

Mozilla's CA bundle, copied from PyPI's `certifi` package.

## Why we need it

TouchDesigner ships its own Python interpreter that doesn't trust the
system / Mozilla root CAs by default. Without a CA bundle, every
`urllib.request.urlopen("https://...")` call raises:

    [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:
    unable to get local issuer certificate

## How it's used

`src/demon_ext.py._prepend_vendor_paths()` sets the `SSL_CERT_FILE`
environment variable to point at `cacert.pem`. Python's `ssl` module
reads `SSL_CERT_FILE` lazily on the first HTTPS request, so all
subsequent stdlib HTTPS calls in TD use this bundle.

## How to refresh

```
pip install -U certifi
cp $(python -c 'import certifi; print(certifi.where())') vendor/certifi/cacert.pem
```

Mozilla updates the bundle roughly twice a year. Refresh on any new
demonTD release.
