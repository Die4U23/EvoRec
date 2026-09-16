from dataclasses import replace
from types import SimpleNamespace

import pytest
torch=pytest.importorskip("torch")
pytest.importorskip("sklearn")
import numpy as np

from evorec.research.baselines import Ranking
from evorec.research.content import ContentFeatures
from evorec.research.neural import configure_seed
from evorec.research.protocol import Query
from evorec.research.ranker import (ResidualListRanker, build_pool_inputs, target_positions,
                                  baseline_rankings, predict, listwise_loss, load_ranker, SCALAR_NAMES)


def fixture():
    features=ContentFeatures(("a","b","c","new"),np.eye(4,dtype=np.float32))
    query=Query("q",100,"new",("a",),frozenset({"a"}),True,True)
    pool=build_pool_inputs(features,dict.fromkeys(features.items,1),{"a","b"},{"b":.7},
                           [query],[Ranking(("b","c"))],[Ranking(("new","c"))],pool_k=4)
    return features,query,pool


def test_feature_builder_never_reads_target_labels():
    f,q,p=fixture()
    altered=replace(q,target="b",target_model_cold=False,target_available=False)
    p2=build_pool_inputs(f,dict.fromkeys(f.items,1),{"a","b"},{"b":.7},
                        [altered],[Ranking(("b","c"))],[Ranking(("new","c"))],pool_k=4)
    np.testing.assert_array_equal(p.scalars,p2.scalars)
    np.testing.assert_array_equal(p.items,p2.items)
    assert p.items.tolist()==[[2,3,4,0]]
    assert p.scalars[0,2,4]==1 and p.scalars[0,0,4]==0


def test_missing_target_is_not_injected_into_pool():
    f,q,p=fixture()
    missed=replace(q,target="a")
    assert target_positions(p,f,[missed]).tolist()==[-1]
    assert 1 not in p.items[0]
    assert target_positions(p,f,[q]).tolist()==[2]


@pytest.mark.parametrize("bad", [("a",),("b","b")])
def test_pool_rejects_seen_and_duplicate_candidates(bad):
    f,q,_=fixture()
    with pytest.raises(ValueError):
        build_pool_inputs(f,dict.fromkeys(f.items,1),set(),{},[q],[Ranking(bad)],[Ranking(())])


def test_pool_rejects_equal_time_and_over_budget():
    f,q,_=fixture()
    with pytest.raises(ValueError):
        build_pool_inputs(f,dict.fromkeys(f.items,100),set(),{},[q],[Ranking(("b",))],[Ranking(())])
    with pytest.raises(ValueError):
        build_pool_inputs(f,dict.fromkeys(f.items,1),set(),{},[q],[Ranking(("b",))],[Ranking(("c",))],pool_k=1)


def test_zero_residual_matches_rrf_and_padding_is_masked():
    configure_seed(17)
    f,q,p=fixture()
    model=ResidualListRanker(4,hidden=8,bottleneck=4)
    assert predict(model,p,f,device="cpu")[0].items==baseline_rankings(p,f)[0].items
    scores=model(torch.from_numpy(p.contexts),torch.zeros(1,4,4),torch.from_numpy(p.scalars),torch.from_numpy(p.items>0))
    assert torch.isneginf(scores[0,3])
    assert len(predict(model,p,f,device="cpu")[0].items)==3


def test_listwise_cold_weight_matches_explicit_weighted_mean():
    scores=torch.tensor([[2.,0.],[0.,2.]],requires_grad=True)
    labels=torch.tensor([0,0])
    cold=torch.tensor([False,True])
    ce=torch.nn.functional.cross_entropy(scores,labels,reduction="none")
    loss=listwise_loss(scores,labels,cold,4)
    torch.testing.assert_close(loss,(ce[0]+4*ce[1])/5)
    loss.backward()
    assert torch.isfinite(scores.grad).all()


def test_ranker_learns_relevance_and_keeps_empty_history_fallback():
    configure_seed(17)
    f,q,p=fixture()
    model=ResidualListRanker(4,hidden=16,bottleneck=8)
    optimizer=torch.optim.AdamW(model.parameters(),lr=.025)
    candidates=torch.from_numpy(np.vstack((f.vectors[1:],np.zeros((1,4),dtype=np.float32))))[None]
    contexts=torch.from_numpy(p.contexts)
    scalars=torch.from_numpy(p.scalars)
    mask=torch.from_numpy(p.items>0)
    labels=torch.tensor([2])
    initial=torch.nn.functional.cross_entropy(model(contexts,candidates,scalars,mask),labels).item()
    for _ in range(35):
        optimizer.zero_grad()
        loss=listwise_loss(model(contexts,candidates,scalars,mask),labels,torch.tensor([True]),4)
        loss.backward(); optimizer.step()
    assert loss.item()<initial*.3
    assert predict(model,p,f,device="cpu")[0].items[0]=="new"
    p.contexts[:]=0
    assert predict(model,p,f,device="cpu")[0].items==baseline_rankings(p,f)[0].items


def test_checkpoint_binds_vectors_and_feature_schema(tmp_path):
    f,_,_=fixture()
    config={"hidden":8,"bottleneck":4,"base_scale":8,"residual_scale":4}
    model=ResidualListRanker(4,**config)
    path=tmp_path/"ranker.pt"
    torch.save({"protocol_id":"p","feature_fingerprint":f.fingerprint,"scalar_names":list(SCALAR_NAMES),
                "model_config":config,"state_dict":model.state_dict()},path)
    load_ranker(path,"p",f,config,"cpu")
    with pytest.raises(ValueError):
        load_ranker(path,"other",f,config,"cpu")
    changed=ContentFeatures(f.items,f.vectors[::-1].copy())
    with pytest.raises(ValueError):
        load_ranker(path,"p",changed,config,"cpu")
