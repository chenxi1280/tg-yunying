import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import ts from 'typescript';

// Execute the actual component-local serializers without mounting the UI.
const path = new URL('../src/app/views/TaskCenterView.tsx', import.meta.url);
const source = readFileSync(path, 'utf8');
const tree = ts.createSourceFile(path.pathname, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const names = new Set(['settingsPayload', 'engagementBindingPayload', 'channelIntakePayload',
  'channelScopePayload', 'channelViewProductionPayload', 'channelCommentPayload']);
const declarations = [];
function visit(node) {
  if (ts.isFunctionDeclaration(node) && node.name && names.has(node.name.text)) declarations.push(node.getText(tree));
  ts.forEachChild(node, visit);
}
visit(tree);
assert.equal(declarations.length, names.size);
const javascript = ts.transpileModule(declarations.join('\n'), {
  compilerOptions: { target: ts.ScriptTarget.ES2020 },
}).outputText;
const context = vm.createContext({
  isSimpleSearchClickTask: () => false, fromBeijingDateTimeLocalValue: value => value,
  accountConfig: () => ({}), pacingConfig: () => ({}), failurePolicy: () => ({}),
  csvNumbers: value => value || [], csvStrings: value => value || [], words: value => value,
  CHANNEL_COUNT_JITTER_DEFAULT: 0.15,
});
vm.runInContext(javascript, context);

for (const type of ['channel_view', 'channel_like', 'channel_comment']) {
  test(`${type} edits serialize source controls independently of target fields`, () => {
    const payload = context.settingsPayload(type, {
      name: 'edit', account_group_ids: [1], target_channel_id: 999,
      initial_historical_post_limit: 0, source_expectation_mode: 'promised_daily_sources',
    });
    assert.equal(payload.initial_historical_post_limit, 0);
    assert.equal(payload.source_expectation_mode, 'promised_daily_sources');
    assert.equal(Object.hasOwn(payload, 'target_channel_id'), false);
    assert.equal(payload.engagement_contract_version, 'unified_engagement_v1');
  });
}
