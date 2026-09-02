
from sqlalchemy import text
from app.database import SessionLocal
import json

session = SessionLocal()
sql = "SELECT id, name, type_config FROM tasks WHERE id = '6407d98f-e6af-4df8-a10b-806135bf24ff';"
row = dict(session.execute(text(sql)).mappings().one())
print("ZHENGZHOU LOUFENG TYPE_CONFIG:")
print(json.dumps(row["type_config"], indent=2, ensure_ascii=False))
session.close()
