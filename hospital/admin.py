import json
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils.html import format_html
from .models import User, Team

class UserAdmin(BaseUserAdmin):
    list_display = (
        'id',
        'email',
        'full_name',
        'role',
        'get_team_ids',  # New column to display team IDs
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
        'teams',  # Enables filtering by teams (uses the related name "teams")
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
    
    def get_team_ids(self, obj):
        # obj.teams is the reverse relationship from the Team model's members field
        team_ids = [str(team.team_id) for team in obj.teams.all()]
        return ", ".join(team_ids) if team_ids else "None"
    get_team_ids.short_description = 'Team IDs'

admin.site.register(User, UserAdmin)

class TeamAdmin(admin.ModelAdmin):
    list_display = ('team_id', 'team_name', 'member_list', 'member_count')
    search_fields = ('team_id', 'team_name')
    ordering = ('team_id',)
    filter_horizontal = ('members',)  # Easy widget for editing members

    def member_count(self, obj):
        return obj.members.count()
    member_count.short_description = 'Number of Members'

    def member_list(self, obj):
        return ", ".join([f"{member.full_name} (ID: {member.id})" for member in obj.members.all()])
    member_list.short_description = "Members"

admin.site.register(Team, TeamAdmin)
