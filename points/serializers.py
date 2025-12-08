from rest_framework import serializers
from .models import Minter, Task, Ledger

class MinterSerializer(serializers.ModelSerializer):
    class Meta:
        model = Minter
        fields = '__all__'

class TaskSerializer(serializers.ModelSerializer):
    class Meta:
        model = Task
        fields = '__all__'
        read_only_fields = ('id', 'created_at', 'updated_at', 'closed_at')

class LedgerSerializer(serializers.ModelSerializer):
    class Meta:
        model = Ledger
        fields = '__all__'
        read_only_fields = ('id', 'created_at')

class PointsBalanceSerializer(serializers.Serializer):
    slack_user_id = serializers.CharField()
    annual_balance = serializers.IntegerField()
    lifetime_balance = serializers.IntegerField()
