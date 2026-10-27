"""Explicit response allowlists for staff with read-only mentoring access."""


def fields(data, names):
    return {name: data[name] for name in names.split() if name in data}


def person(data):
    return fields(data, 'id name') if data else None


def invite(data):
    return {**fields(data, 'id status created_at expires_at used_at revoked_at'),
            **{key: person(data.get(key)) for key in ('user', 'created_by', 'revoked_by')}}


def application(data):
    result = {**fields(data, 'id state headline session_minutes price_roo location_type submitted_at rejected_at'),
              'user': person(data.get('user')), 'reviewed_by': person(data.get('reviewed_by'))}
    if 'profile' in data:
        result['profile'] = fields(data['profile'],
            'id name avatar_url headline bio topics timezone away_dates session_minutes price_roo '
            'location_type brief_questions is_active availability')
    return result


def mentor(data):
    return {**fields(data, 'id headline is_active paused_at approved_at price_roo session_minutes location_type sessions_count'),
            'user': person(data.get('user')), 'reviewed_by': person(data.get('reviewed_by'))}


def session(data):
    # Provider error text can contain addresses or meeting URLs. Only expose status.
    return {**fields(data, 'id status starts_at ends_at price_roo location_type meeting_status '
                     'notification_status requested_at responded_at cancelled_at flags'),
            'mentor': person(data.get('mentor')), 'founder': person(data.get('founder'))}
