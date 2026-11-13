"""会后重聚类：散会后拿整场的声纹重新分一次是谁说的。"""
import numpy as np

from avatar.recluster import Item, recluster

rng = np.random.default_rng(7)
A, B, C = (rng.normal(size=64) for _ in range(3))


def voice(base, noise=0.25):
    return (base + rng.normal(scale=noise, size=64)).astype(np.float32)


def items(spec):
    """spec：[(说话人原编号, 真实声音, 秒数), …]，行号从 1 起。"""
    return [Item(line_id=i, seconds=sec, emb=None if base is None else voice(base), speaker_id=sid)
            for i, (sid, base, sec) in enumerate(spec, 1)]


def test_one_person_split_into_two_is_merged_back():
    # 会上 A 的声音被拆成了说话人 1 和说话人 3
    its = items([(1, A, 4), (2, B, 4), (3, A, 3), (1, A, 5), (2, B, 3), (3, A, 4)])
    changes = recluster(its)
    assert changes == {3: 1, 6: 1}


def test_two_people_merged_into_one_are_split_apart():
    its = items([(1, A, 4), (1, B, 4), (1, A, 3), (1, B, 5), (1, A, 3), (1, B, 3)])
    changes = recluster(its)
    new = {changes.get(i, 1) for i in (2, 4, 6)}
    assert changes and len(new) == 1 and new != {1}           # B 的几句归到同一位新说话人
    assert all(i not in changes for i in (1, 3, 5))


def test_short_lines_follow_the_closest_voice_and_lines_without_voice_stay():
    its = items([(1, A, 4), (2, B, 4), (1, A, 4), (2, B, 4), (1, B, 1.2), (2, None, 0.8)])
    changes = recluster(its)
    assert changes == {5: 2}                                   # 短句的声音更像 B
    assert 6 not in changes                                    # 没有声纹的不动


def test_user_decisions_are_never_overwritten():
    its = items([(1, A, 4), (2, B, 4), (1, A, 3), (2, B, 3), (2, A, 4)])
    its[4].fixed = True                                        # 用户亲手把这句判给了说话人 2
    assert recluster(its) == {}


def test_a_new_person_gets_a_new_number():
    its = items([(1, A, 4), (1, A, 4), (1, C, 4), (1, C, 4), (1, A, 4)])
    changes = recluster(its)
    assert set(changes) == {3, 4} and len(set(changes.values())) == 1 and next(iter(changes.values())) < 0


def test_too_little_to_go_on_changes_nothing():
    assert recluster(items([(1, A, 4), (2, B, 1.0)])) == {}
    assert recluster([]) == {}
