import numpy as np

from avatar.turns import Turn, cut_text, offsets, plan, runs

A, B, C = np.eye(3, dtype=np.float32)


def turn(line_id, seconds, voices, speaker_id, emb=None, fixed=False):
    return Turn(line_id, seconds, np.stack(voices) if voices else np.zeros((0, 3), dtype=np.float32),
                emb, speaker_id, fixed)


def test_a_line_gets_a_window_every_three_quarters_of_a_second():
    assert offsets(1.4) == []
    assert offsets(1.5) == [0.0]
    assert offsets(4.0) == [0.0, 0.75, 1.5, 2.25]


def test_a_stray_window_or_two_is_folded_into_its_neighbours():
    assert runs([1, 1, 2, 1, 1], 2) == [(1, 5)]
    assert runs([1, 1, 1, 2, 2, 2], 2) == [(1, 3), (2, 3)]
    assert runs([1, 1, 1, 2], 2) == [(1, 4)]


def test_a_line_with_two_voices_is_cut_between_them():
    changes, splits = plan([
        turn(1, 2.0, [A], 1),
        turn(2, 4.0, [A, A, B, B], 1),          # 会上断成一句，其实是两个人接着说
        turn(3, 2.0, [B], 2),
    ])
    assert changes == {}
    assert splits == {2: [(0.0, 1.875, 1), (1.875, 4.0, 2)]}


def test_whole_lines_move_and_lines_the_owner_fixed_stay_put():
    changes, splits = plan([
        turn(1, 3.0, [A, A, A], 1),
        turn(2, 3.0, [B, B, B], 1),             # 会上被并给了第一位
        turn(3, 3.0, [B, B, B], 2),
        turn(4, 3.0, [A, A, A], 2, fixed=True),  # 用户亲手判给了第二位
    ])
    assert changes == {2: 2} and splits == {}


def test_a_short_line_follows_the_voice_it_sounds_like():
    changes, _ = plan([
        turn(1, 3.0, [A, A, A], 1),
        turn(2, 3.0, [B, B, B], 2),
        turn(3, 1.0, [], 1, emb=B),              # 太短切不出小窗：看整句声纹
        turn(4, 1.0, [], 2, emb=(A + B) / 2),    # 两边一样像：不动
        turn(5, 1.0, [], 9, emb=(A + B) / 2),    # 原来那位已经没有了：跟最像的走
    ])
    assert changes[3] == 2 and 4 not in changes and changes[5] in (1, 2)


def test_a_voice_nobody_had_before_becomes_a_new_speaker():
    changes, splits = plan([
        turn(1, 3.0, [A, A, A], 1),
        turn(2, 3.0, [C, C, C], 1),
        turn(3, 3.0, [A, A, A], 1),
    ])
    assert changes == {2: -1} and splits == {}


def test_the_words_go_to_whichever_part_they_were_said_in():
    tokens = ["好", "的", "，", "那", "我", "做", "注", "册", "吧", "。"]
    times = [0.2, 0.5, 1.9, 2.0, 2.2, 2.5, 2.8, 3.1, 3.4, 3.7]
    got = cut_text(tokens, times, [(0.0, 1.875, 1), (1.875, 4.0, 2)])
    assert got == [(0.0, 1.875, 1, "好的，"), (1.875, 4.0, 2, "那我做注册吧。")]


def test_a_part_with_no_words_is_not_a_line_of_its_own():
    tokens = ["先", "做", "登", "录", "。"]
    times = [0.1, 0.3, 0.5, 0.7, 2.5]
    got = cut_text(tokens, times, [(0.0, 2.0, 1), (2.0, 4.0, 2), (4.0, 6.0, 1)])
    assert got == [(0.0, 6.0, 1, "先做登录。")]
