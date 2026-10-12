"""Test-only router for two independent API databases in one test process."""
from contextvars import ContextVar
active_api_database=ContextVar('active_api_database',default='default')


class FederationTestRouter:
    def db_for_read(self,model,**hints):
        return active_api_database.get()
    def db_for_write(self,model,**hints):
        return active_api_database.get()
    def allow_relation(self,obj1,obj2,**hints):
        return obj1._state.db==obj2._state.db
    def allow_migrate(self,db,app_label,model_name=None,**hints):
        active_api_database.set(db)
        return True
