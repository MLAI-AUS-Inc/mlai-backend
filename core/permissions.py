from rest_framework import permissions

class IsOwnerOrTeammateOrSuperuser(permissions.BasePermission):
    """
    Custom permission to allow:
    - Owners to read and edit their own profile.
    - Teammates to read each other's profiles.
    - Superusers to read and edit everything.
    """

    def has_object_permission(self, request, view, obj):
        # Read permissions are allowed to any request,
        # so we'll always allow GET, HEAD or OPTIONS requests.
        # BUT we want to restrict read access to teammates only (or owner/admin)
        
        # Check for superuser
        if request.user.is_superuser:
            return True

        # Check for owner
        if obj == request.user:
            return True

        # Check for teammate (Read-only)
        if request.method in permissions.SAFE_METHODS:
            # Check hospital teams
            if hasattr(request.user, 'hospital_teams') and hasattr(obj, 'hospital_teams'):
                user_teams = request.user.hospital_teams.all()
                obj_teams = obj.hospital_teams.all()
                if any(team in user_teams for team in obj_teams):
                    return True
            
            # Check esafety teams
            if hasattr(request.user, 'esafety_teams') and hasattr(obj, 'esafety_teams'):
                user_teams = request.user.esafety_teams.all()
                obj_teams = obj.esafety_teams.all()
                if any(team in user_teams for team in obj_teams):
                    return True

        # Write permissions are only allowed to the owner or superuser.
        return False


class HasAPIKey(permissions.BasePermission):
    """
    Allows access if the X-API-Key header matches INTERNAL_API_KEY in settings.
    Used for securing service-to-service endpoints (e.g. Roo agent -> Backend).
    """
    def has_permission(self, request, view):
        from django.conf import settings
        
        api_key = request.META.get('HTTP_X_API_KEY')
        internal_key = getattr(settings, 'INTERNAL_API_KEY', None)
        
        if not internal_key:
            # If no key configured on backend, deny all API access to be safe
            return False
            
        return api_key == internal_key
