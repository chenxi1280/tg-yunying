"""Full material-list service on migrated PostgreSQL, without Action ORM hydration."""
from sqlalchemy.orm import Session

from app.database import engine
from app.models import Action, Material, Task, Tenant
from app.services.ai_config import list_materials

TENANT_ID = 990_011
MATERIAL_ID = 990_011


def test_material_list_aggregates_actions_without_hydration():
    assert engine.dialect.name == 'postgresql'
    with Session(engine) as session:
        session.add(Tenant(id=TENANT_ID, name='material reference integration'))
        session.flush()
        session.add(Material(id=MATERIAL_ID, tenant_id=TENANT_ID, title='reference',
                             material_type='图片', content='https://example.test/image.png'))
        session.add(Task(id='reference-integration-task', tenant_id=TENANT_ID,
                         name='reference', type='channel_view'))
        session.flush()
        session.add(Action(id='reference-integration-action', tenant_id=TENANT_ID,
                           task_id='reference-integration-task', task_type='channel_view',
                           action_type='view_message', payload={'material_id': MATERIAL_ID},
                           result={'material_ids': [MATERIAL_ID, MATERIAL_ID]}))
        session.flush()
        session.expunge_all()
        materials = list_materials(session, TENANT_ID)
        assert len(materials) == 1
        assert materials[0].reference_summary.action_count == 1
        assert materials[0].reference_summary.total_count == 1
        assert not any(isinstance(value, Action) for value in session.identity_map.values())
