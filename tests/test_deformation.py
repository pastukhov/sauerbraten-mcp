"""Deformed geometry is checked through the real wire protocol and save/reload."""
import json
import uuid
from pathlib import Path
import pytest
from pydantic import ValidationError
from conftest import CLIENT
from test_live_editor import game, token
from sauer_mcp.bridge import BridgeError
from sauer_mcp.models import validate, schemas
from sauer_mcp.service import EditorService

SEL = dict(origin=[16, 16, 528], count=[1, 1, 1], grid=8, orient=5)
R, C = [1, 2, 0], [2, 0, 1]


def test_deformation_schemas():
  assert {'geometry_face_set', 'geometry_vertex_set'} <= schemas().keys()
  base = dict(map_epoch=str(uuid.uuid4()), expected_revision='1', request_id=str(uuid.uuid4()), selection=SEL)
  assert validate('geometry_face_set', dict(base, depths=[0, 8, 0, 8]))['depths'] == [0, 8, 0, 8]
  assert validate('geometry_vertex_set', dict(base, corner=[1, 0, 1], position=[7, 1, 7]))['corner'] == [1, 0, 1]
  for method, field, values in [
    ('geometry_face_set','depths', [[0,8,8], [0,8,8,9], [True,8,8,8], [0.,8,8,8]]),
    ('geometry_vertex_set','corner', [[0,0,2], [False,0,1], [0,1]]),
    ('geometry_vertex_set','position', [[-1,0,8], [0,0,9], [0,0,float('nan')], [0,0,True]]),
  ]:
    for value in values:
      args = dict(base, **({'depths':[8]*4} if method.endswith('face_set') else {'corner':[1]*3,'position':[8]*3}))
      args[field] = value
      with pytest.raises(ValidationError): validate(method, args)
  with pytest.raises(ValidationError): validate('geometry_face_set', dict(base, depths=[8]*4, script='quit'))
  contract = json.loads((Path(__file__).parents[1]/'contracts/v1.json').read_text())
  assert contract['tools'] == schemas()


async def fill(game, selection=SEL):
  tex = (await game.call('texture_catalog', {'limit':2}))['entries'][1]['ref']
  await game.call('geometry_fill', await token(game, selection=selection, mode='solid', texture_ref=tex))


async def leaves(game, selection=SEL):
  return (await game.call('region_read', {'bounds':{'origin':selection['origin'], 'extent':[n*selection['grid'] for n in selection['count']]}}))['leaves']


@pytest.mark.live
@pytest.mark.parametrize('orient', range(6))
async def test_face_coordinates_and_history(game, orient):
  s = dict(SEL, orient=orient)
  await fill(game, s)
  await game.call('material_apply', await token(game, selection=s, material='glass'))
  before = (await leaves(game))[0]
  state = await game.call('editor_state', {})
  values = [1,2,1,2] if orient%2==0 else [6,7,6,7]
  args = await token(game, selection=s, depths=values)
  result = await game.call('geometry_face_set', args)
  assert await game.call('geometry_face_set', args) == result
  expected = [128]*12
  axis, end = orient//2, orient%2
  for i,n in enumerate(values): expected[4*axis+i] = (n<<4) if end else 128|n
  actual = (await leaves(game))[0]
  assert actual['edges'] == expected
  assert actual['textures'] == before['textures'] and actual['material'] == before['material']
  assert (await game.call('editor_state',{}))['selection'] == state['selection']
  with pytest.raises(BridgeError,match='REQUEST_ID_REUSED'):
    await game.call('geometry_face_set',dict(args, depths=[5]*4))
  with pytest.raises(BridgeError,match='STALE_STATE'):
    await game.call('geometry_face_set',dict(args,request_id=str(uuid.uuid4())))
  await game.call('history_undo', await token(game))
  assert (await leaves(game))[0]['edges'] == [128]*12
  # An absolute no-op must retain the redo branch and revision.
  state = await game.call('editor_state',{})
  await game.call('geometry_face_set',await token(game, selection=s, depths=[8 if end else 0]*4))
  after = await game.call('editor_state',{})
  assert after['revision']==state['revision'] and after['can_redo']
  await game.call('history_redo',await token(game))
  assert (await leaves(game))[0]['edges'] == expected


@pytest.mark.live
@pytest.mark.parametrize('corner', [[x,y,z] for x in range(2) for y in range(2) for z in range(2)])
async def test_vertex_three_axes_history(game,corner):
  await fill(game)
  position = [7 if n else 1 for n in corner]
  await game.call('geometry_vertex_set',await token(game,selection=SEL,corner=corner,position=position))
  expected=[128]*12
  for d in range(3):
    index=4*d+2*corner[C[d]]+corner[R[d]]
    expected[index]=(position[d]<<4) if corner[d] else 128|position[d]
  assert (await leaves(game))[0]['edges']==expected
  await game.call('history_undo',await token(game))
  assert (await leaves(game))[0]['edges']==[128]*12
  await game.call('history_redo',await token(game))
  assert (await leaves(game))[0]['edges']==expected


@pytest.mark.live
async def test_deformation_rejection_atomicity(game):
  s=dict(SEL,count=[2,1,1])
  await fill(game,s)
  # Second cube has a higher lower face: the same new upper face is invalid there.
  second=dict(SEL,origin=[24,16,528],orient=4)
  await game.call('geometry_face_set',await token(game,selection=second,depths=[7]*4))
  before=await leaves(game,s)
  state=await game.call('editor_state',{})
  for method, args, error in [
    ('geometry_face_set',dict(selection=s,depths=[6]*4),'INVALID_GEOMETRY'),
    ('geometry_vertex_set',dict(selection=SEL,corner=[1,1,1],position=[1,1,1]),'INVALID_GEOMETRY'),
    ('geometry_face_set',dict(selection=SEL,depths=[0]*4),'INVALID_GEOMETRY'),
    ('geometry_face_set',dict(selection=dict(SEL,grid=16,origin=[16,16,528]),depths=[4]*4),'GEOMETRY_RESOLUTION_MISMATCH'),
    ('geometry_face_set',dict(selection=dict(SEL,origin=[64,64,640]),depths=[4]*4),'GEOMETRY_RESOLUTION_MISMATCH'),
    ('geometry_vertex_set',dict(selection=SEL,corner=[0,0,0],position=[9,0,0]),'INVALID_ARGUMENT'),
    ('geometry_vertex_set',dict(selection=SEL,corner=[True,0,0],position=[1,0,0]),'INVALID_ARGUMENT'),
  ]:
    with pytest.raises(BridgeError,match=error): await game.call(method,await token(game,**args))
    after=await game.call('editor_state',{})
    assert all(after[k]==state[k] for k in ['revision','dirty','allocnodes','can_undo','can_redo','selection'])
    assert await leaves(game,s)==before


@pytest.mark.live
async def test_deformed_sdk_save_reload(game):
  from mcp.shared.memory import create_connected_server_and_client_session
  from sauer_mcp.server import create_server
  await fill(game)
  service=EditorService(game, CLIENT)
  async with create_connected_server_and_client_session(create_server(game.path,service=service)) as sdk:
    names={t.name for t in (await sdk.list_tools()).tools}
    assert {'geometry_face_set','geometry_vertex_set'} <= names
    result=await sdk.call_tool('geometry_face_set',await token(game,selection=SEL,depths=[8,8,0,0]))
    assert not result.isError
    result=await sdk.call_tool('geometry_vertex_set',await token(game,selection=dict(SEL,origin=[16,16,528]),corner=[1,0,1],position=[8,0,8]))
    assert not result.isError
  before=await leaves(game)
  saved=await service.call('map_save_as',await token(game,slug='wedge'))
  await service.call('map_open',await token(game,artifact_id=saved['artifact_id']))
  assert [(c['origin'],c['size'],c['edges'],c['material']) for c in await leaves(game)]==[(c['origin'],c['size'],c['edges'],c['material']) for c in before]
