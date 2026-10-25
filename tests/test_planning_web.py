"""The move of plans out of the browser, and the vocabulary the page draws with.

Plans used to live in `localStorage` and are files now. The move happens once,
in whichever browser opens the new page first, and what is tested is that it
brings the right plans: an evening's edits over the outline, the outline only
where nothing was ever loaded, nothing into an unrelated library, and nothing
a second time.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parents[1] / 'src/splice/web/index.html'


def _script() -> str:
    return re.search(r'<script>(.*)</script>', PAGE.read_text(), re.S).group(1)


def _node(tmp_path, source):
    path = tmp_path / 'page.cjs'
    path.write_text(source)
    result = subprocess.run(['node', str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(not shutil.which('node'), reason='no node on this machine')
def test_plans_move_into_the_archive_once_and_bring_the_right_ones(tmp_path):
    script = _script()
    storage = script[script.index('const PLANS_KEY ='):script.index('/* The seven states')]
    moving = script[script.index('/* --- plan storage'):script.index('async function newPlan(')]
    test = """
const assert = require('node:assert/strict');
const data = new Map();
const localStorage = {
  getItem: key => data.has(key) ? data.get(key) : null,
  setItem: (key, value) => data.set(key, value),
};
const window = { addEventListener: () => {} };
const document = { querySelectorAll: () => [] };
const refreshPlanUsage = () => {};
const renderView = () => {};
const toast = message => { throw Error(message); };
let state, archive, seed, live, imports;
const reset = () => {
  state = { plans: [], planSeed: null, tab: 'Plans' };
  archive = null; imports = 0;
  seed = { id: 'trip', plans: [{ id: 'one', title: 'First', moments: [{ id: 'a' }] }] };
  live = [{ id: 'a' }];
};
const api = async (path, opts = {}) => {
  if (path === '/api/plans/seed') return structuredClone(seed);
  if (path === '/api/segments?') return live;
  if (path === '/api/states') return { segments: {}, counts: {} };
  if (path === '/api/plans') return { exists: archive !== null, plans: archive || [] };
  if (path === '/api/plans/import') {
    if (archive !== null) throw Object.assign(Error('already'), { status: 409 });
    imports += 1;
    archive = JSON.parse(opts.body).plans.map((p, i) => ({ ...p, number: i + 1 }));
    return { imported: archive.length };
  }
  throw Error('unexpected ' + path);
};
""" + storage + moving + """
(async () => {
  // An evening of edits in this browser wins over the outline it began as.
  reset();
  data.set(PLANS_KEY, JSON.stringify([{ id: 'one', title: 'Rewritten', moments: [{ id: 'a' }] },
                                      { id: 'mine', title: 'My own' }]));
  await loadPlans();
  assert.deepEqual(state.plans.map(p => p.title), ['Rewritten', 'My own']);
  assert.ok(data.get(PLANS_MOVED_KEY));
  assert.ok(data.get(PLANS_KEY), 'the old copy is left where it was');

  // A second load, or a second tab, does not import again.
  archive = [];
  await loadPlans();
  assert.equal(imports, 1);
  assert.deepEqual(state.plans, []);

  // A browser that never loaded the outline starts from it.
  reset(); data.clear();
  await loadPlans();
  assert.deepEqual(state.plans.map(p => p.id), ['one']);

  // One that loaded it and deleted every plan made a decision.
  reset(); data.clear();
  data.set(PLAN_SEEDS_KEY, JSON.stringify(['trip']));
  await loadPlans();
  assert.equal(imports, 0);
  assert.equal(archive, null, 'nothing marks the move done, so a later import is still possible');

  // A different media library starts without this family's ideas.
  reset(); data.clear(); live = [{ id: 'unrelated' }];
  await loadPlans();
  assert.equal(imports, 0);
  assert.deepEqual(state.plans, []);
  assert.equal(state.plansReady, true);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    _node(tmp_path, test)


@pytest.mark.skipif(not shutil.which('node'), reason='no node on this machine')
def test_a_failed_import_leaves_the_plans_unreadable_rather_than_empty(tmp_path):
    """"Nothing planned yet" invites a New plan, and the first new plan makes
    the directory that marks the move as done -- stranding what was in the
    browser."""
    script = _script()
    storage = script[script.index('const PLANS_KEY ='):script.index('/* The seven states')]
    moving = script[script.index('/* --- plan storage'):script.index('async function newPlan(')]
    _node(tmp_path, """
const assert = require('node:assert/strict');
const data = new Map([['plans', JSON.stringify([{ id: 'mine', title: 'Mine' }])]]);
const localStorage = { getItem: k => data.get(k) ?? null, setItem: (k, v) => data.set(k, v) };
const window = { addEventListener: () => {} };
const refreshPlanUsage = () => {};
const state = { plans: [], tab: 'Plans' };
const api = async path => {
  if (path === '/api/plans/seed') return null;
  if (path === '/api/plans') return { exists: false, plans: [] };
  if (path === '/api/states') return { segments: {}, counts: {} };
  if (path === '/api/plans/import') throw Object.assign(Error('a plan did not parse'), { status: 400 });
  throw Error(path);
};
""" + storage + moving + """
loadPlans().then(() => {
  assert.equal(state.plansReady, false);
  assert.match(state.plansError, /did not parse/);
  assert.equal(data.get(PLANS_MOVED_KEY), undefined);
}).catch(error => { console.error(error); process.exitCode = 1; });
""")


def test_every_state_has_an_icon_and_every_stage_a_colour_of_its_own_or_a_state_s():
    """A state with no icon draws as unseen, which is the one wrong answer a
    badge can give quietly. Idea and gathering both propose their moments,
    and drawn in that one state's colour the page could not tell them apart."""
    script = _script()
    states = re.findall(r"^\s*\['(\w+)', '\w+', '[^']*'\],$",
                        script[script.index('const STATES = ['):script.index('const STAGES = [')], re.M)
    stages = re.findall(r"^\s*\['(\w+)', '\w+', '(\w+)', '[^']*'\],$",
                        script[script.index('const STAGES = ['):script.index('const stageInfo')], re.M)
    icons = set(re.findall(r"^\s*(\w+): '<", script[script.index('const ICONS = {'):
                                                       script.index('function stateBadge(')], re.M))
    css = PAGE.read_text()

    from splice import plans
    looks = {colour for _, colour in stages}
    assert set(states) == set(plans.STATES)
    assert icons == set(states) | looks
    assert [s for s, _ in stages] == list(plans.STAGES)
    assert len(looks) == len(stages)
    for name in icons:
        assert f'--st-{name}:' in css and f'.st-{name}' in css
    for name in states:
        assert f'[data-state="{name}"]' in css


@pytest.mark.skipif(not shutil.which('node'), reason='no node on this machine')
def test_a_drag_moves_reels_round_the_posted_ones_and_never_moves_those(tmp_path):
    """Episode 2 went out as episode 2. Dragging the others past it is
    planning; renumbering it would be rewriting what happened."""
    script = _script()
    fn = script[script.index('function reorderAround('):script.index('function scheduleTile(')]
    _node(tmp_path, """
const assert = require('node:assert/strict');
""" + fn + """
const order = ['a', 'b', 'c', 'd'];
const none = new Set(), b = new Set(['b']);
const move = (posted, dragged, target, after) => reorderAround(order, posted, dragged, target, after).join('');

assert.equal(move(none, 'd', 'a', false), 'dabc');
assert.equal(move(none, 'a', 'c', true), 'bcad');
assert.equal(move(none, 'a', 'a', true), 'abcd');

// b is posted and stays second, whatever happens round it.
assert.equal(move(b, 'd', 'a', false), 'dbac');
assert.equal(move(b, 'a', 'd', true), 'cbda');
// Dropped onto the posted reel: the open slot on that side of it.
assert.equal(move(b, 'd', 'b', false), 'dbac');
assert.equal(move(b, 'a', 'b', true), 'cbad');
// The posted reel itself does not move.
assert.equal(move(b, 'b', 'd', true), 'abcd');
""")


@pytest.mark.skipif(not shutil.which('node'), reason='no node on this machine')
def test_the_page_moves_a_plan_by_the_same_rule_the_server_saves(tmp_path):
    """A card moves the moment its stage changes, before the save comes back,
    so the page's rule has to be the server's: furthest along first, each
    group in its saved order, and a plan changing stage at the end of its new
    group. The ready ones go by their number on the Schedule, and one made
    ready before its number arrives goes after every reel that has one.
    `test_plans` holds the server to the same cases."""
    script = _script()
    fn = script[script.index('const PLAN_RANK ='):script.index('/* The server\'s numbers')]
    _node(tmp_path, """
const assert = require('node:assert/strict');
const plansView = { slots: new Map([['d', 1], ['b', 2]]) };
""" + fn + """
const plans = [
  { id: 'a', stage: 'shelved', order: 0 }, { id: 'b', stage: 'ready', posted: true, order: 1 },
  { id: 'c', stage: 'idea', order: 2 }, { id: 'd', stage: 'ready', order: 3 },
  { id: 'e', stage: 'gathering', order: 4 }, { id: 'f', stage: 'idea', order: 5 },
];
const ids = () => plans.map(p => p.id + p.order).join(' ');
const move = (id, stage) => {
  const plan = plans.find(p => p.id === id);
  plan.stage = stage;
  restackPlans(plans, plan);
};
restackPlans(plans);
assert.equal(ids(), 'd0 b1 e2 c3 f4 a5', 'd goes out first, though b was thought of first');
plansView.slots = new Map([['b', 1], ['d', 2]]);   // dragged on the Schedule
restackPlans(plans);
assert.equal(ids(), 'b0 d1 e2 c3 f4 a5');

// Ready before the Schedule has numbered it: after every reel that is numbered.
move('c', 'ready');
assert.equal(ids(), 'b0 d1 c2 e3 f4 a5');
move('c', 'idea');

// f was thought of after c; up to gathering, it goes after e, which was there first.
move('f', 'gathering');
assert.equal(ids(), 'b0 d1 e2 f3 c4 a5');

// Back down, d joins the end of the ideas rather than the place it had.
move('d', 'idea');
assert.equal(ids(), 'b0 e1 f2 c3 d4 a5');
""")


@pytest.mark.skipif(not shutil.which('node'), reason='no node on this machine')
def test_a_menu_near_the_bottom_of_the_window_opens_upwards(tmp_path):
    """The stage pill on the last plan in the window opened its menu under the
    bottom edge, where a fixed menu cannot be scrolled to. It opens above when
    it does not fit below and there is more room above, and it is capped to
    the room on whichever side it opens."""
    script = _script()
    fn = script[script.index('function menuPlacement('):script.index('function popMenu(')]
    _node(tmp_path, """
const assert = require('node:assert/strict');
""" + fn + """
const pill = top => ({ top, bottom: top + 24 });

// Room below: where it always opened.
assert.deepEqual(menuPlacement(pill(100), 200, 800),
  { top: '130px', bottom: '', maxHeight: '624px' });
// The last plan in the window: above the pill, its bottom 6 px over the pill's top.
assert.deepEqual(menuPlacement(pill(740), 200, 800),
  { top: '', bottom: '66px', maxHeight: '624px' });
// Too tall for either side, and more room below: below, and it scrolls.
assert.deepEqual(menuPlacement(pill(300), 700, 800),
  { top: '330px', bottom: '', maxHeight: '462px' });
// Too tall for either side, and more room above: above, capped to that room.
assert.deepEqual(menuPlacement(pill(500), 700, 800),
  { top: '', bottom: '306px', maxHeight: '486px' });
""")


@pytest.mark.skipif(not shutil.which('node'), reason='no node on this machine')
def test_a_duplicate_keeps_the_words_and_the_pool_and_none_of_the_decisions(tmp_path):
    """A copy of a posted reel is a new idea, not a second record of what went
    out. And its takes come unticked, because a take ticked in two plans wears
    the contradiction ring and every one of them would."""
    script = _script()
    fn = script[script.index('function copyOfPlan('):script.index('async function duplicatePlan(')]
    _node(tmp_path, """
const assert = require('node:assert/strict');
""" + fn + """
const plan = { id: 'p', revision: 'r', order: 3, title: 'Wi-Fi', desc: 'A blessing', strand: 'Tech',
  target: '60 sec', stage: 'ready', posted: true, week: 1, slot: 2, moments: [
    { id: 'a#1', title: 'Router', stem: 'a', start: 1, end: 5, in_edit: true, note: 'open on this', reattached: true },
    { id: 'a#6', title: 'Speed test', stem: 'a', start: 6, end: 9, in_edit: false, note: '' }] };
const copy = copyOfPlan(plan);
assert.deepEqual(copy, {
  title: 'Wi-Fi (copy)', desc: 'A blessing', strand: 'Tech', target: '60 sec',
  moments: [
    { id: 'a#1', title: 'Router', stem: 'a', start: 1, end: 5, in_edit: false, note: 'open on this' },
    { id: 'a#6', title: 'Speed test', stem: 'a', start: 6, end: 9, in_edit: false, note: '' }] });
assert.equal(plan.moments[0].in_edit, true, 'the original keeps its ticks');
assert.equal(copyOfPlan({ title: '', moments: [] }).title, 'Untitled plan (copy)');
""")


@pytest.mark.skipif(not shutil.which('node'), reason='no node on this machine')
def test_undo_is_per_page_and_a_new_action_forgets_what_was_undone(tmp_path):
    """⌘Z on Schedule must not take a moment out of a plan on Segments, out of
    sight. A step that fails is about a state that is gone, and is dropped
    rather than tried again; one whose guard says no stays where it was."""
    script = _script()
    undo = script[script.index('// --- undo ---'):script.index('/* --- plans --- */')]
    _node(tmp_path, """
const assert = require('node:assert/strict');
const handlers = [];
const document = { addEventListener: (type, fn) => handlers.push(fn) };
const window = { scrollY: 0, scrollTo: () => {} };
const $ = s => s === '#dlg' ? { open: false } : null;
const el = (tag, attrs, text) => ({ ...attrs, text, attributes: {},
  setAttribute(k, v) { this.attributes[k] = v; } });
const said = [];
const toast = message => said.push(message);
const renderView = async () => {};
const state = { tab: 'Plans' };
""" + undo + """
(async () => {
  let value = 0;
  const set = to => () => { value = to; };
  value = 1; remember('one', set(0), set(1));
  value = 2; remember('two', set(1), set(2));
  state.tab = 'Schedule';
  await undoStep(true);
  assert.equal(value, 2, 'Schedule has nothing of Plans to undo');
  assert.equal(said.pop(), 'Nothing to undo');

  state.tab = 'Plans';
  paintUndo();
  assert.equal(undoButtons[0].title, 'Undo: two (⌘Z)');
  await undoStep(true);
  await undoStep(true);
  assert.equal(value, 0);
  await undoStep(false);
  assert.equal(value, 1);
  assert.equal(said.at(-1), 'Redone: one');

  remember('three', set(1), set(3));
  await undoStep(false);
  assert.equal(said.pop(), 'Nothing to redo', 'a new action forgets the undone ones');

  remember('broken', () => { throw Error('that plan is gone'); }, set(4));
  await undoStep(true);
  assert.match(said.pop(), /Could not undo broken: that plan is gone/);
  assert.equal(undoLog.get('Plans').undone.length, 0);

  let allowed = false;
  remember('guarded', set(3), set(5), { guard: () => allowed });
  await undoStep(true);
  assert.equal(undoLog.get('Plans').done.at(-1).label, 'guarded');
  allowed = true;
  await undoStep(true);
  assert.equal(undoLog.get('Plans').done.at(-1).label, 'three');

  // Recording while an entry plays would make the replay a new action.
  remember('replays', () => remember('echo', set(0), set(0)), set(0));
  await undoStep(true);
  assert.equal(undoLog.get('Plans').done.at(-1).label, 'three');

  // The keys: ⌘Z and ⇧⌘Z / Ctrl+Y, but not inside a text box.
  const key = (k, opts = {}) => {
    const e = { key: k, metaKey: true, ctrlKey: false, altKey: false, shiftKey: false,
      target: { closest: () => opts.typing || null }, preventDefault() { this.stopped = true; }, ...opts };
    handlers.forEach(fn => fn(e));
    return e.stopped === true;
  };
  assert.equal(key('z', { typing: {} }), false, 'the text box keeps its own undo');
  assert.equal(key('Z', { shiftKey: true }), true);
  assert.equal(key('y', { metaKey: false, ctrlKey: true }), true);
  assert.equal(key('x'), false);
  state.tab = 'Clips';
  assert.equal(key('z'), false, 'pages without a history leave the key alone');
})().catch(error => { console.error(error); process.exitCode = 1; });
""")
