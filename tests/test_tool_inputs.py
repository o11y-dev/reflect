import json

from reflect.tool_inputs import skill_read_paths, tool_invocations


def test_literal_wrapper_pairs_each_result_without_using_outer_success():
    code = 'text(await tools.exec_command({cmd:"git status",max_output_tokens:500})); text(await tools.exec_command({cmd:"cat /a/skills/check/SKILL.md"}));'
    output = [{'type':'input_text','text':'Script completed\nOutput:\n'},
              {'type':'input_text','text':json.dumps({'exit_code':0,'output':'clean'})},
              {'type':'input_text','text':json.dumps({'exit_code':1,'output':'missing'})}]
    row = {'tool_name':'exec','raw_attrs_json':{'gen_ai.client.tool.input':code,'process.exit.code':0},'output_preview_redacted':json.dumps(output),'status':'ok'}
    calls = tool_invocations(row)
    assert [c['status'] for c in calls] == ['ok','error']
    assert [c['wrapper_index'] for c in calls] == [0,1]
    row['output_preview_redacted'] = 'truncated output'
    assert tool_invocations(row) == [row]


def test_nonliteral_or_conditional_code_is_not_interpreted():
    for code in ['if (ok) text(await tools.exec_command({cmd:"git commit"}));',
                 'text(await tools.exec_command({cmd:command}));',
                 'text("tools.exec_command({cmd:git commit})");']:
        row = {'tool_name':'exec','input_preview_redacted':code}
        assert tool_invocations(row) == [row]


def test_skill_read_commands_distinguish_composite_results_and_mentions():
    path = '/a/skills/check/SKILL.md'
    for command in [f'echo {path}', f'rg {path} .', f'cat /tmp/input > {path}', f'sed s/foo/{path} /tmp/input', f'python -c "print(\'{path}\')"']:
        assert skill_read_paths({'command':command}, 'exec_command') == []
    assert skill_read_paths({'command':f'cat {path}'}, 'exec_command') == [(path, True)]
    assert skill_read_paths({'command':f'cat {path}\ngit status'}, 'exec_command') == [(path, False)]


def test_relative_skill_paths_use_invocation_directory(tmp_path):
    from reflect.tool_inputs import resolve_skill_path
    assert resolve_skill_path('skills/check/SKILL.md', {'gen_ai.client.tool.input': {'workdir': str(tmp_path)}}, '/other') == str(tmp_path / 'skills/check/SKILL.md')
    assert resolve_skill_path('~/skills/check/SKILL.md', {}, '/other').endswith('/skills/check/SKILL.md')
    assert '/other/~' not in resolve_skill_path('~/skills/check/SKILL.md', {}, '/other')
