def normalize_domain(domain: str) -> str:
    """
    Normalize a domain by removing protocol, www, and any trailing path segments.
    Examples:
        https://www.example.com/about -> example.com
        http://example.com -> example.com
        www.example.com -> example.com
    """
    if not domain:
        return domain

    domain = domain.strip().lower()
    if domain.startswith("https://"):
        domain = domain[8:]
    elif domain.startswith("http://"):
        domain = domain[7:]

    if domain.startswith("www."):
        domain = domain[4:]

    if "/" in domain:
        domain = domain.split("/")[0]

    return domain
