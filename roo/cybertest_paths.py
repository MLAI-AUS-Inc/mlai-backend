"""Small shared URL contract; origins and application paths are distinct settings."""
def base_path(value=''):
    if value not in ('', '/cybertest'):
        raise ValueError('Invalid Cybertest base path.')
    return value
