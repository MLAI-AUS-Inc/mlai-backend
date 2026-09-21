# Apple transaction verification root

`AppleRootCA-G3.cer` is Apple's public DER trust anchor, downloaded from
<https://www.apple.com/certificateauthority/AppleRootCA-G3.cer> on 2026-09-21.
It is not a private key or developer credential.

SHA-256: `63343abfb89a6a03ebb57e9b3f5fa7be7c4f5c756f3017b3a8c488c3653e9179`.

Apple's official server library validates the certificate chain, signature,
application identity and environment, with online revocation checks enabled.
New roots require a reviewed update from Apple's PKI site. Never accept a
client-provided root or disable verification to get a purchase through.
