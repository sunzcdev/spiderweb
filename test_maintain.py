import sqlite3
import os
import json
from src.spiderweb.engine.maintain import MaintainService

# Setup minimal db
db_path = "test.db"
if os.path.exists(db_path): os.remove(db_path)
conn = sqlite3.connect(db_path)
conn.execute("CREATE TABLE entities (canonical_name TEXT PRIMARY KEY, entity_type TEXT, source TEXT)")
conn.execute("CREATE TABLE relations (id INTEGER PRIMARY KEY, entity_a TEXT, entity_b TEXT, relation_type TEXT, weight REAL, metadata TEXT DEFAULT '{}')")
conn.execute("INSERT INTO entities VALUES ('Book1', 'work', 'auto'), ('EntityA', 'person', 'auto')")
conn.commit()
conn.close()

# Test
svc = MaintainService(db_path, {})
print(svc.register("NewEntity", "person", book="Book1"))
print(svc.connect("EntityA", "NewEntity", "MENTIONS"))
print(svc.annotate("EntityA", "NewEntity", "test note"))
