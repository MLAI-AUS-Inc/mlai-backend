import json
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils.html import format_html
from .models import (
    User
)

class UserAdmin(BaseUserAdmin):
    list_display = (
        'email',
        'full_name',
        'role',
        'is_active',
        'is_staff',
        'is_superuser',
        'date_joined',
    )
    list_filter = (
        'role',
        'is_active',
        'is_staff',
        'is_superuser',
    )
    
    fieldsets = (
        (None, {'fields': ('email', 'password')}),
        ('Personal Info', {'fields': ('full_name', 'role')}),
        ('Permissions', {'fields': ('is_active', 'is_staff', 'is_superuser', 'groups', 'user_permissions')}),
        ('Important Dates', {'fields': ('last_login', 'date_joined')}),
    )
    
    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': (
                'email',
                'full_name',
                'password1',
                'password2',
                'role',
                'is_active',
                'is_staff',
                'is_superuser',
            ),
        }),
    )
    
    search_fields = ('id', 'email', 'full_name')
    ordering = ('full_name',)

admin.site.register(User, UserAdmin)
