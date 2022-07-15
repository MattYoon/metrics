# Copyright The PyTorch Lightning team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from functools import partial
from typing import Callable, Optional

import numpy as np
import pytest
import torch
from sklearn.metrics import f1_score, fbeta_score
from torch import Tensor

from unittests.classification.inputs import _binary_cases, _multiclass_cases, _multilabel_cases
from unittests.helpers import seed_all
from unittests.helpers.testers import NUM_BATCHES, NUM_CLASSES, THRESHOLD, MetricTester, inject_ignore_index

seed_all(42)


def _sk_fbeta_binary(preds, target, ignore_index, multidim_average):
    if multidim_average == "global":
        preds = preds.view(-1).numpy()
        target = target.view(-1).numpy()
    else:
        preds = preds.numpy()
        target = target.numpy()

    if np.issubdtype(preds.dtype, np.floating):
        if not ((0 < preds) & (preds < 1)).all():
            preds = sigmoid(preds)
        preds = (preds >= THRESHOLD).astype(np.uint8)

    if multidim_average == "global":
        if ignore_index is not None:
            idx = target == ignore_index
            target = target[~idx]
            preds = preds[~idx]
        tn, fp, fn, tp = sk_confusion_matrix(y_true=target, y_pred=preds, labels=[0, 1]).ravel()
        return np.array([tp, fp, tn, fn, tp + fn])
    else:
        res = []
        for pred, true in zip(preds, target):
            pred = pred.flatten()
            true = true.flatten()
            if ignore_index is not None:
                idx = true == ignore_index
                true = true[~idx]
                pred = pred[~idx]
            tn, fp, fn, tp = sk_confusion_matrix(y_true=true, y_pred=pred, labels=[0, 1]).ravel()
            res.append(np.array([tp, fp, tn, fn, tp + fn]))
        return np.stack(res)


@pytest.mark.parametrize("input", _binary_cases)
class TestBinaryFBeta(MetricTester):
    @pytest.mark.parametrize("ignore_index", [None, 0, -1])
    @pytest.mark.parametrize("multidim_average", ["global", "samplewise"])
    @pytest.mark.parametrize("ddp", [False, True])
    def test_binary_fbeta(self, ddp, input, ignore_index, multidim_average):
        preds, target = input
        if ignore_index == -1:
            target = inject_ignore_index(target, ignore_index)
        if multidim_average == "samplewise" and preds.ndim < 3:
            pytest.skip("samplewise and non-multidim arrays are not valid")
        if multidim_average == "samplewise" and ddp:
            pytest.skip("samplewise and ddp give different order than non ddp")

        self.run_class_metric_test(
            ddp=ddp,
            preds=preds,
            target=target,
            metric_class=BinaryFBeta,
            sk_metric=partial(_sk_fbeta_binary, ignore_index=ignore_index, multidim_average=multidim_average),
            metric_args={"threshold": THRESHOLD, "ignore_index": ignore_index, "multidim_average": multidim_average},
        )

    @pytest.mark.parametrize("ignore_index", [None, 0, -1])
    @pytest.mark.parametrize("multidim_average", ["global", "samplewise"])
    def test_binary_fbeta_functional(self, input, ignore_index, multidim_average):
        preds, target = input
        if ignore_index == -1:
            target = inject_ignore_index(target, ignore_index)
        if multidim_average == "samplewise" and preds.ndim < 3:
            pytest.skip("samplewise and non-multidim arrays are not valid")

        self.run_functional_metric_test(
            preds=preds,
            target=target,
            metric_functional=binary_fbeta,
            sk_metric=partial(_sk_fbeta_binary, ignore_index=ignore_index, multidim_average=multidim_average),
            metric_args={
                "threshold": THRESHOLD,
                "ignore_index": ignore_index,
                "multidim_average": multidim_average,
            },
        )

    def test_binary_fbeta_differentiability(self, input):
        preds, target = input
        self.run_differentiability_test(
            preds=preds,
            target=target,
            metric_module=BinaryFBeta,
            metric_functional=binary_fbeta,
            metric_args={"threshold": THRESHOLD},
        )

    @pytest.mark.parametrize("dtype", [torch.half, torch.double])
    def test_binary_fbeta_half_cpu(self, input, dtype):
        preds, target = input
        if dtype == torch.half and not _TORCH_GREATER_EQUAL_1_6:
            pytest.xfail(reason="half support of core ops not support before pytorch v1.6")
        if (preds < 0).any() and dtype == torch.half:
            pytest.xfail(reason="torch.sigmoid in metric does not support cpu + half precision")
        self.run_precision_test_cpu(
            preds=preds,
            target=target,
            metric_module=BinaryFBeta,
            metric_functional=binary_fbeta,
            metric_args={"threshold": THRESHOLD},
            dtype=dtype,
        )

    @pytest.mark.skipif(not torch.cuda.is_available(), reason="test requires cuda")
    @pytest.mark.parametrize("dtype", [torch.half, torch.double])
    def test_binary_fbeta_half_gpu(self, input, dtype):
        preds, target = input
        self.run_precision_test_gpu(
            preds=preds,
            target=target,
            metric_module=BinaryFBeta,
            metric_functional=binary_fbeta,
            metric_args={"threshold": THRESHOLD},
            dtype=dtype,
        )


def _sk_fbeta_multiclass(preds, target, ignore_index, multidim_average, average):
    if preds.ndim == target.ndim + 1:
        preds = torch.argmax(preds, 1)
    if multidim_average == "global":
        preds = preds.numpy().flatten()
        target = target.numpy().flatten()

        if ignore_index is not None:
            idx = target == ignore_index
            target = target[~idx]
            preds = preds[~idx]
        confmat = sk_confusion_matrix(y_true=target, y_pred=preds, labels=list(range(NUM_CLASSES)))
        tp = np.diag(confmat)
        fp = confmat.sum(0) - tp
        fn = confmat.sum(1) - tp
        tn = confmat.sum() - (fp + fn + tp)

        res = np.stack([tp, fp, tn, fn, tp + fn], 1)
        if average == "micro":
            return res.sum(0)
        elif average == "macro":
            return res.mean(0)
        elif average == "weighted":
            w = tp + fn
            return (res * (w / w.sum()).reshape(-1, 1)).sum(0)
        elif average is None or average == "none":
            return res

    else:
        preds = preds.numpy()
        target = target.numpy()

        res = []
        for pred, true in zip(preds, target):
            pred = pred.flatten()
            true = true.flatten()

            if ignore_index is not None:
                idx = true == ignore_index
                true = true[~idx]
                pred = pred[~idx]
            confmat = sk_confusion_matrix(y_true=true, y_pred=pred, labels=list(range(NUM_CLASSES)))
            tp = np.diag(confmat)
            fp = confmat.sum(0) - tp
            fn = confmat.sum(1) - tp
            tn = confmat.sum() - (fp + fn + tp)
            r = np.stack([tp, fp, tn, fn, tp + fn], 1)
            if average == "micro":
                res.append(r.sum(0))
            elif average == "macro":
                res.append(r.mean(0))
            elif average == "weighted":
                w = tp + fn
                res.append((r * (w / w.sum()).reshape(-1, 1)).sum(0))
            elif average is None or average == "none":
                res.append(r)
        return np.stack(res, 0)


@pytest.mark.parametrize("input", _multiclass_cases)
class TestMulticlassFBeta(MetricTester):
    @pytest.mark.parametrize("ignore_index", [None, 0, -1])
    @pytest.mark.parametrize("multidim_average", ["global", "samplewise"])
    @pytest.mark.parametrize("average", ["micro", "macro", None])
    @pytest.mark.parametrize("ddp", [True, False])
    def test_multiclass_fbeta(self, ddp, input, ignore_index, multidim_average, average):
        preds, target = input
        if ignore_index == -1:
            target = inject_ignore_index(target, ignore_index)
        if multidim_average == "samplewise" and target.ndim < 3:
            pytest.skip("samplewise and non-multidim arrays are not valid")
        if multidim_average == "samplewise" and ddp:
            pytest.skip("samplewise and ddp give different order than non ddp")

        self.run_class_metric_test(
            ddp=ddp,
            preds=preds,
            target=target,
            metric_class=MulticlassFBeta,
            sk_metric=partial(
                _sk_fbeta_multiclass,
                ignore_index=ignore_index,
                multidim_average=multidim_average,
                average=average,
            ),
            metric_args={
                "ignore_index": ignore_index,
                "multidim_average": multidim_average,
                "average": average,
                "num_classes": NUM_CLASSES,
            },
        )

    @pytest.mark.parametrize("ignore_index", [None, 0, -1])
    @pytest.mark.parametrize("multidim_average", ["global", "samplewise"])
    @pytest.mark.parametrize("average", ["micro", "macro", None])
    def test_multiclass_fbeta_functional(self, input, ignore_index, multidim_average, average):
        preds, target = input
        if ignore_index == -1:
            target = inject_ignore_index(target, ignore_index)
        if multidim_average == "samplewise" and target.ndim < 3:
            pytest.skip("samplewise and non-multidim arrays are not valid")

        self.run_functional_metric_test(
            preds=preds,
            target=target,
            metric_functional=multiclass_fbeta,
            sk_metric=partial(
                _sk_fbeta_multiclass,
                ignore_index=ignore_index,
                multidim_average=multidim_average,
                average=average,
            ),
            metric_args={
                "ignore_index": ignore_index,
                "multidim_average": multidim_average,
                "average": average,
                "num_classes": NUM_CLASSES,
            },
        )

    def test_multiclass_fbeta_differentiability(self, input):
        preds, target = input
        self.run_differentiability_test(
            preds=preds,
            target=target,
            metric_module=MulticlassFBeta,
            metric_functional=multiclass_fbeta,
            metric_args={"num_classes": NUM_CLASSES},
        )

    @pytest.mark.parametrize("dtype", [torch.half, torch.double])
    def test_multiclass_fbeta_half_cpu(self, input, dtype):
        preds, target = input
        if dtype == torch.half and not _TORCH_GREATER_EQUAL_1_6:
            pytest.xfail(reason="half support of core ops not support before pytorch v1.6")
        if (preds < 0).any() and dtype == torch.half:
            pytest.xfail(reason="torch.sigmoid in metric does not support cpu + half precision")
        self.run_precision_test_cpu(
            preds=preds,
            target=target,
            metric_module=MulticlassFBeta,
            metric_functional=multiclass_fbeta,
            metric_args={"num_classes": NUM_CLASSES},
            dtype=dtype,
        )

    @pytest.mark.skipif(not torch.cuda.is_available(), reason="test requires cuda")
    @pytest.mark.parametrize("dtype", [torch.half, torch.double])
    def test_multiclass_fbeta_half_gpu(self, input, dtype):
        preds, target = input
        self.run_precision_test_gpu(
            preds=preds,
            target=target,
            metric_module=MulticlassFBeta,
            metric_functional=multiclass_fbeta,
            metric_args={"num_classes": NUM_CLASSES},
            dtype=dtype,
        )


_mc_k_target = torch.tensor([0, 1, 2])
_mc_k_preds = torch.tensor([[0.35, 0.4, 0.25], [0.1, 0.5, 0.4], [0.2, 0.1, 0.7]])


@pytest.mark.parametrize(
    "k, preds, target, average, expected",
    [
        (1, _mc_k_preds, _mc_k_target, "micro", torch.tensor([2, 1, 5, 1, 3])),
        (2, _mc_k_preds, _mc_k_target, "micro", torch.tensor([3, 3, 3, 0, 3])),
        (1, _mc_k_preds, _mc_k_target, None, torch.tensor([[0, 1, 1], [0, 1, 0], [2, 1, 2], [1, 0, 0], [1, 1, 1]])),
        (2, _mc_k_preds, _mc_k_target, None, torch.tensor([[1, 1, 1], [1, 1, 1], [1, 1, 1], [0, 0, 0], [1, 1, 1]])),
    ],
)
def test_top_k_multiclass(k, preds, target, average, expected):
    """A simple test to check that top_k works as expected."""
    class_metric = MulticlassFBeta(top_k=k, average=average, num_classes=3)
    class_metric.update(preds, target)

    assert torch.allclose(class_metric.compute().long(), expected.T)
    assert torch.allclose(multiclass_fbeta(preds, target, top_k=k, average=average, num_classes=3).long(), expected.T)


def _sk_fbeta_multilabel(preds, target, ignore_index, multidim_average, average):
    preds = preds.numpy()
    target = target.numpy()
    if np.issubdtype(preds.dtype, np.floating):
        if not ((0 < preds) & (preds < 1)).all():
            preds = sigmoid(preds)
        preds = (preds >= THRESHOLD).astype(np.uint8)
    preds = preds.reshape(*preds.shape[:2], -1)
    target = target.reshape(*target.shape[:2], -1)
    if multidim_average == "global":
        fbeta = []
        for i in range(preds.shape[1]):
            p, t = preds[:, i].flatten(), target[:, i].flatten()
            if ignore_index is not None:
                idx = t == ignore_index
                t = t[~idx]
                p = p[~idx]
            tn, fp, fn, tp = sk_confusion_matrix(t, p, labels=[0, 1]).ravel()
            fbeta.append(np.array([tp, fp, tn, fn, tp + fn]))
        res = np.stack(fbeta, axis=0)

        if average == "micro":
            return res.sum(0)
        elif average == "macro":
            return res.mean(0)
        elif average == "weighted":
            w = res[:, 0] + res[:, 3]
            return (res * (w / w.sum()).reshape(-1, 1)).sum(0)
        elif average is None or average == "none":
            return res
    else:
        fbeta = []
        for i in range(preds.shape[0]):
            scores = []
            for j in range(preds.shape[1]):
                pred, true = preds[i, j], target[i, j]
                if ignore_index is not None:
                    idx = true == ignore_index
                    true = true[~idx]
                    pred = pred[~idx]
                tn, fp, fn, tp = sk_confusion_matrix(true, pred, labels=[0, 1]).ravel()
                scores.append(np.array([tp, fp, tn, fn, tp + fn]))
            fbeta.append(np.stack(scores, 1))
        res = np.stack(fbeta, 0)
        if average == "micro":
            return res.sum(-1)
        elif average == "macro":
            return res.mean(-1)
        elif average == "weighted":
            w = res[:, 0, :] + res[:, 3, :]
            return (res * (w / w.sum())[:, np.newaxis]).sum(-1)
        elif average is None or average == "none":
            return np.moveaxis(res, 1, -1)


@pytest.mark.parametrize("input", _multilabel_cases)
class TestMultilabelFBeta(MetricTester):
    @pytest.mark.parametrize("ddp", [True, False])
    @pytest.mark.parametrize("ignore_index", [None, 0, -1])
    @pytest.mark.parametrize("multidim_average", ["global", "samplewise"])
    @pytest.mark.parametrize("average", ["micro", "macro", None])
    def test_multilabel_fbeta(self, ddp, input, ignore_index, multidim_average, average):
        preds, target = input
        if ignore_index == -1:
            target = inject_ignore_index(target, ignore_index)
        if multidim_average == "samplewise" and preds.ndim < 4:
            pytest.skip("samplewise and non-multidim arrays are not valid")
        if multidim_average == "samplewise" and ddp:
            pytest.skip("samplewise and ddp give different order than non ddp")

        self.run_class_metric_test(
            ddp=ddp,
            preds=preds,
            target=target,
            metric_class=MultilabelFBeta,
            sk_metric=partial(
                _sk_fbeta_multilabel,
                ignore_index=ignore_index,
                multidim_average=multidim_average,
                average=average,
            ),
            metric_args={
                "num_labels": NUM_CLASSES,
                "threshold": THRESHOLD,
                "ignore_index": ignore_index,
                "multidim_average": multidim_average,
                "average": average,
            },
        )

    @pytest.mark.parametrize("ignore_index", [None, 0, -1])
    @pytest.mark.parametrize("multidim_average", ["global", "samplewise"])
    @pytest.mark.parametrize("average", ["micro", "macro", None])
    def test_multilabel_fbeta_functional(self, input, ignore_index, multidim_average, average):
        preds, target = input
        if ignore_index == -1:
            target = inject_ignore_index(target, ignore_index)
        if multidim_average == "samplewise" and preds.ndim < 4:
            pytest.skip("samplewise and non-multidim arrays are not valid")

        self.run_functional_metric_test(
            preds=preds,
            target=target,
            metric_functional=multilabel_fbeta,
            sk_metric=partial(
                _sk_fbeta_multilabel,
                ignore_index=ignore_index,
                multidim_average=multidim_average,
                average=average,
            ),
            metric_args={
                "num_labels": NUM_CLASSES,
                "threshold": THRESHOLD,
                "ignore_index": ignore_index,
                "multidim_average": multidim_average,
                "average": average,
            },
        )

    def test_multilabel_fbeta_differentiability(self, input):
        preds, target = input
        self.run_differentiability_test(
            preds=preds,
            target=target,
            metric_module=MultilabelFBeta,
            metric_functional=multilabel_fbeta,
            metric_args={"num_labels": NUM_CLASSES, "threshold": THRESHOLD},
        )

    @pytest.mark.parametrize("dtype", [torch.half, torch.double])
    def test_multilabel_fbeta_half_cpu(self, input, dtype):
        preds, target = input
        if dtype == torch.half and not _TORCH_GREATER_EQUAL_1_6:
            pytest.xfail(reason="half support of core ops not support before pytorch v1.6")
        if (preds < 0).any() and dtype == torch.half:
            pytest.xfail(reason="torch.sigmoid in metric does not support cpu + half precision")
        self.run_precision_test_cpu(
            preds=preds,
            target=target,
            metric_module=MultilabelFBeta,
            metric_functional=multilabel_fbeta,
            metric_args={"num_labels": NUM_CLASSES, "threshold": THRESHOLD},
            dtype=dtype,
        )

    @pytest.mark.skipif(not torch.cuda.is_available(), reason="test requires cuda")
    @pytest.mark.parametrize("dtype", [torch.half, torch.double])
    def test_multilabel_fbeta_half_gpu(self, input, dtype):
        preds, target = input
        self.run_precision_test_gpu(
            preds=preds,
            target=target,
            metric_module=MultilabelFBeta,
            metric_functional=multilabel_fbeta,
            metric_args={"num_labels": NUM_CLASSES, "threshold": THRESHOLD},
            dtype=dtype,
        )


# -------------------------- Old stuff --------------------------

# def _sk_fbeta_f1(preds, target, sk_fn, num_classes, average, multiclass, ignore_index, mdmc_average=None):
#     if average == "none":
#         average = None
#     if num_classes == 1:
#         average = "binary"

#     labels = list(range(num_classes))
#     try:
#         labels.remove(ignore_index)
#     except ValueError:
#         pass

#     sk_preds, sk_target, _ = _input_format_classification(
#         preds, target, THRESHOLD, num_classes=num_classes, multiclass=multiclass
#     )
#     sk_preds, sk_target = sk_preds.numpy(), sk_target.numpy()
#     sk_scores = sk_fn(sk_target, sk_preds, average=average, zero_division=0, labels=labels)

#     if len(labels) != num_classes and not average:
#         sk_scores = np.insert(sk_scores, ignore_index, np.nan)

#     return sk_scores


# def _sk_fbeta_f1_multidim_multiclass(
#     preds, target, sk_fn, num_classes, average, multiclass, ignore_index, mdmc_average
# ):
#     preds, target, _ = _input_format_classification(
#         preds, target, threshold=THRESHOLD, num_classes=num_classes, multiclass=multiclass
#     )

#     if mdmc_average == "global":
#         preds = torch.transpose(preds, 1, 2).reshape(-1, preds.shape[1])
#         target = torch.transpose(target, 1, 2).reshape(-1, target.shape[1])

#         return _sk_fbeta_f1(preds, target, sk_fn, num_classes, average, False, ignore_index)
#     if mdmc_average == "samplewise":
#         scores = []

#         for i in range(preds.shape[0]):
#             pred_i = preds[i, ...].T
#             target_i = target[i, ...].T
#             scores_i = _sk_fbeta_f1(pred_i, target_i, sk_fn, num_classes, average, False, ignore_index)

#             scores.append(np.expand_dims(scores_i, 0))

#         return np.concatenate(scores).mean(axis=0)


# @pytest.mark.parametrize(
#     "metric_class, metric_fn",
#     [
#         (partial(FBetaScore, beta=2.0), partial(fbeta_score_pl, beta=2.0)),
#         (F1Score, f1_score_pl),
#     ],
# )
# @pytest.mark.parametrize(
#     "average, mdmc_average, num_classes, ignore_index, match_str",
#     [
#         ("wrong", None, None, None, "`average`"),
#         ("micro", "wrong", None, None, "`mdmc"),
#         ("macro", None, None, None, "number of classes"),
#         ("macro", None, 1, 0, "ignore_index"),
#     ],
# )
# def test_wrong_params(metric_class, metric_fn, average, mdmc_average, num_classes, ignore_index, match_str):
#     with pytest.raises(ValueError, match=match_str):
#         metric_class(
#             average=average,
#             mdmc_average=mdmc_average,
#             num_classes=num_classes,
#             ignore_index=ignore_index,
#         )

#     with pytest.raises(ValueError, match=match_str):
#         metric_fn(
#             _input_binary.preds[0],
#             _input_binary.target[0],
#             average=average,
#             mdmc_average=mdmc_average,
#             num_classes=num_classes,
#             ignore_index=ignore_index,
#         )


# @pytest.mark.parametrize(
#     "metric_class, metric_fn",
#     [
#         (partial(FBetaScore, beta=2.0), partial(fbeta_score_pl, beta=2.0)),
#         (F1Score, f1_score_pl),
#     ],
# )
# def test_zero_division(metric_class, metric_fn):
#     """Test that zero_division works correctly (currently should just set to 0)."""

#     preds = torch.tensor([1, 2, 1, 1])
#     target = torch.tensor([2, 0, 2, 1])

#     cl_metric = metric_class(average="none", num_classes=3)
#     cl_metric(preds, target)

#     result_cl = cl_metric.compute()
#     result_fn = metric_fn(preds, target, average="none", num_classes=3)

#     assert result_cl[0] == result_fn[0] == 0


# @pytest.mark.parametrize(
#     "metric_class, metric_fn",
#     [
#         (partial(FBetaScore, beta=2.0), partial(fbeta_score_pl, beta=2.0)),
#         (F1Score, f1_score_pl),
#     ],
# )
# def test_no_support(metric_class, metric_fn):
#     """This tests a rare edge case, where there is only one class present.

#     in target, and ignore_index is set to exactly that class - and the
#     average method is equal to 'weighted'.

#     This would mean that the sum of weights equals zero, and would, without
#     taking care of this case, return NaN. However, the reduction function
#     should catch that and set the metric to equal the value of zero_division
#     in this case (zero_division is for now not configurable and equals 0).
#     """

#     preds = torch.tensor([1, 1, 0, 0])
#     target = torch.tensor([0, 0, 0, 0])

#     cl_metric = metric_class(average="weighted", num_classes=2, ignore_index=0)
#     cl_metric(preds, target)

#     result_cl = cl_metric.compute()
#     result_fn = metric_fn(preds, target, average="weighted", num_classes=2, ignore_index=0)

#     assert result_cl == result_fn == 0


# @pytest.mark.parametrize(
#     "metric_class, metric_fn",
#     [
#         (partial(FBetaScore, beta=2.0), partial(fbeta_score_pl, beta=2.0)),
#         (F1Score, f1_score_pl),
#     ],
# )
# @pytest.mark.parametrize(
#     "ignore_index, expected", [(None, torch.tensor([1.0, np.nan])), (0, torch.tensor([np.nan, np.nan]))]
# )
# def test_class_not_present(metric_class, metric_fn, ignore_index, expected):
#     """This tests that when metric is computed per class and a given class is not present in both the `preds` and
#     `target`, the resulting score is `nan`."""
#     preds = torch.tensor([0, 0, 0])
#     target = torch.tensor([0, 0, 0])
#     num_classes = 2

#     # test functional
#     result_fn = metric_fn(preds, target, average=AverageMethod.NONE, num_classes=num_classes, ignore_index=ignore_index)
#     assert torch.allclose(expected, result_fn, equal_nan=True)

#     # test class
#     cl_metric = metric_class(average=AverageMethod.NONE, num_classes=num_classes, ignore_index=ignore_index)
#     cl_metric(preds, target)
#     result_cl = cl_metric.compute()
#     assert torch.allclose(expected, result_cl, equal_nan=True)


# @pytest.mark.parametrize(
#     "metric_class, metric_fn, sk_fn",
#     [
#         (partial(FBetaScore, beta=2.0), partial(fbeta_score_pl, beta=2.0), partial(fbeta_score, beta=2.0)),
#         (F1Score, f1_score_pl, f1_score),
#     ],
# )
# @pytest.mark.parametrize("average", ["micro", "macro", None, "weighted", "samples"])
# @pytest.mark.parametrize("ignore_index", [None, 0])
# @pytest.mark.parametrize(
#     "preds, target, num_classes, multiclass, mdmc_average, sk_wrapper",
#     [
#         (_input_binary_logits.preds, _input_binary_logits.target, 1, None, None, _sk_fbeta_f1),
#         (_input_binary_prob.preds, _input_binary_prob.target, 1, None, None, _sk_fbeta_f1),
#         (_input_binary.preds, _input_binary.target, 1, False, None, _sk_fbeta_f1),
#         (_input_mlb_logits.preds, _input_mlb_logits.target, NUM_CLASSES, None, None, _sk_fbeta_f1),
#         (_input_mlb_prob.preds, _input_mlb_prob.target, NUM_CLASSES, None, None, _sk_fbeta_f1),
#         (_input_mlb.preds, _input_mlb.target, NUM_CLASSES, False, None, _sk_fbeta_f1),
#         (_input_mcls_logits.preds, _input_mcls_logits.target, NUM_CLASSES, None, None, _sk_fbeta_f1),
#         (_input_mcls_prob.preds, _input_mcls_prob.target, NUM_CLASSES, None, None, _sk_fbeta_f1),
#         (_input_mcls.preds, _input_mcls.target, NUM_CLASSES, None, None, _sk_fbeta_f1),
#         (_input_mdmc.preds, _input_mdmc.target, NUM_CLASSES, None, "global", _sk_fbeta_f1_multidim_multiclass),
#         (
#             _input_mdmc_prob.preds,
#             _input_mdmc_prob.target,
#             NUM_CLASSES,
#             None,
#             "global",
#             _sk_fbeta_f1_multidim_multiclass,
#         ),
#         (_input_mdmc.preds, _input_mdmc.target, NUM_CLASSES, None, "samplewise", _sk_fbeta_f1_multidim_multiclass),
#         (
#             _input_mdmc_prob.preds,
#             _input_mdmc_prob.target,
#             NUM_CLASSES,
#             None,
#             "samplewise",
#             _sk_fbeta_f1_multidim_multiclass,
#         ),
#     ],
# )
# class TestFBeta(MetricTester):
#     @pytest.mark.parametrize("ddp", [True, False])
#     @pytest.mark.parametrize("dist_sync_on_step", [True, False])
#     def test_fbeta_f1(
#         self,
#         ddp: bool,
#         dist_sync_on_step: bool,
#         preds: Tensor,
#         target: Tensor,
#         sk_wrapper: Callable,
#         metric_class: Metric,
#         metric_fn: Callable,
#         sk_fn: Callable,
#         multiclass: Optional[bool],
#         num_classes: Optional[int],
#         average: str,
#         mdmc_average: Optional[str],
#         ignore_index: Optional[int],
#     ):
#         if num_classes == 1 and average != "micro":
#             pytest.skip("Only test binary data for 'micro' avg (equivalent of 'binary' in sklearn)")

#         if ignore_index is not None and preds.ndim == 2:
#             pytest.skip("Skipping ignore_index test with binary inputs.")

#         if average == "weighted" and ignore_index is not None and mdmc_average is not None:
#             pytest.skip("Ignore special case where we are ignoring entire sample for 'weighted' average")

#         self.run_class_metric_test(
#             ddp=ddp,
#             preds=preds,
#             target=target,
#             metric_class=metric_class,
#             sk_metric=partial(
#                 sk_wrapper,
#                 sk_fn=sk_fn,
#                 average=average,
#                 num_classes=num_classes,
#                 multiclass=multiclass,
#                 ignore_index=ignore_index,
#                 mdmc_average=mdmc_average,
#             ),
#             dist_sync_on_step=dist_sync_on_step,
#             metric_args={
#                 "num_classes": num_classes,
#                 "average": average,
#                 "threshold": THRESHOLD,
#                 "multiclass": multiclass,
#                 "ignore_index": ignore_index,
#                 "mdmc_average": mdmc_average,
#             },
#         )

#     def test_fbeta_f1_functional(
#         self,
#         preds: Tensor,
#         target: Tensor,
#         sk_wrapper: Callable,
#         metric_class: Metric,
#         metric_fn: Callable,
#         sk_fn: Callable,
#         multiclass: Optional[bool],
#         num_classes: Optional[int],
#         average: str,
#         mdmc_average: Optional[str],
#         ignore_index: Optional[int],
#     ):
#         if num_classes == 1 and average != "micro":
#             pytest.skip("Only test binary data for 'micro' avg (equivalent of 'binary' in sklearn)")

#         if ignore_index is not None and preds.ndim == 2:
#             pytest.skip("Skipping ignore_index test with binary inputs.")

#         if average == "weighted" and ignore_index is not None and mdmc_average is not None:
#             pytest.skip("Ignore special case where we are ignoring entire sample for 'weighted' average")

#         self.run_functional_metric_test(
#             preds,
#             target,
#             metric_functional=metric_fn,
#             sk_metric=partial(
#                 sk_wrapper,
#                 sk_fn=sk_fn,
#                 average=average,
#                 num_classes=num_classes,
#                 multiclass=multiclass,
#                 ignore_index=ignore_index,
#                 mdmc_average=mdmc_average,
#             ),
#             metric_args={
#                 "num_classes": num_classes,
#                 "average": average,
#                 "threshold": THRESHOLD,
#                 "multiclass": multiclass,
#                 "ignore_index": ignore_index,
#                 "mdmc_average": mdmc_average,
#             },
#         )

#     def test_fbeta_f1_differentiability(
#         self,
#         preds: Tensor,
#         target: Tensor,
#         sk_wrapper: Callable,
#         metric_class: Metric,
#         metric_fn: Callable,
#         sk_fn: Callable,
#         multiclass: Optional[bool],
#         num_classes: Optional[int],
#         average: str,
#         mdmc_average: Optional[str],
#         ignore_index: Optional[int],
#     ):
#         if num_classes == 1 and average != "micro":
#             pytest.skip("Only test binary data for 'micro' avg (equivalent of 'binary' in sklearn)")

#         if ignore_index is not None and preds.ndim == 2:
#             pytest.skip("Skipping ignore_index test with binary inputs.")

#         if average == "weighted" and ignore_index is not None and mdmc_average is not None:
#             pytest.skip("Ignore special case where we are ignoring entire sample for 'weighted' average")

#         self.run_differentiability_test(
#             preds,
#             target,
#             metric_functional=metric_fn,
#             metric_module=metric_class,
#             metric_args={
#                 "num_classes": num_classes,
#                 "average": average,
#                 "threshold": THRESHOLD,
#                 "multiclass": multiclass,
#                 "ignore_index": ignore_index,
#                 "mdmc_average": mdmc_average,
#             },
#         )


# _mc_k_target = torch.tensor([0, 1, 2])
# _mc_k_preds = torch.tensor([[0.35, 0.4, 0.25], [0.1, 0.5, 0.4], [0.2, 0.1, 0.7]])
# _ml_k_target = torch.tensor([[0, 1, 0], [1, 1, 0], [0, 0, 0]])
# _ml_k_preds = torch.tensor([[0.9, 0.2, 0.75], [0.1, 0.7, 0.8], [0.6, 0.1, 0.7]])


# @pytest.mark.parametrize(
#     "metric_class, metric_fn",
#     [
#         (partial(FBetaScore, beta=2.0), partial(fbeta_score_pl, beta=2.0)),
#         (F1Score, fbeta_score_pl),
#     ],
# )
# @pytest.mark.parametrize(
#     "k, preds, target, average, expected_fbeta, expected_f1",
#     [
#         (1, _mc_k_preds, _mc_k_target, "micro", torch.tensor(2 / 3), torch.tensor(2 / 3)),
#         (2, _mc_k_preds, _mc_k_target, "micro", torch.tensor(5 / 6), torch.tensor(2 / 3)),
#         (1, _ml_k_preds, _ml_k_target, "micro", torch.tensor(0.0), torch.tensor(0.0)),
#         (2, _ml_k_preds, _ml_k_target, "micro", torch.tensor(5 / 18), torch.tensor(2 / 9)),
#     ],
# )
# def test_top_k(
#     metric_class,
#     metric_fn,
#     k: int,
#     preds: Tensor,
#     target: Tensor,
#     average: str,
#     expected_fbeta: Tensor,
#     expected_f1: Tensor,
# ):
#     """A simple test to check that top_k works as expected.

#     Just a sanity check, the tests in FBeta should already guarantee the corectness of results.
#     """
#     class_metric = metric_class(top_k=k, average=average, num_classes=3)
#     class_metric.update(preds, target)

#     if class_metric.beta != 1.0:
#         result = expected_fbeta
#     else:
#         result = expected_f1

#     assert torch.isclose(class_metric.compute(), result)
#     assert torch.isclose(metric_fn(preds, target, top_k=k, average=average, num_classes=3), result)


# @pytest.mark.parametrize("ignore_index", [None, 2])
# @pytest.mark.parametrize("average", ["micro", "macro", "weighted"])
# @pytest.mark.parametrize(
#     "metric_class, metric_functional, sk_fn",
#     [
#         (partial(FBetaScore, beta=2.0), partial(fbeta_score_pl, beta=2.0), partial(fbeta_score, beta=2.0)),
#         (F1Score, f1_score_pl, f1_score),
#     ],
# )
# def test_same_input(metric_class, metric_functional, sk_fn, average, ignore_index):
#     preds = _input_miss_class.preds
#     target = _input_miss_class.target
#     preds_flat = torch.cat(list(preds), dim=0)
#     target_flat = torch.cat(list(target), dim=0)

#     mc = metric_class(num_classes=NUM_CLASSES, average=average, ignore_index=ignore_index)
#     for i in range(NUM_BATCHES):
#         mc.update(preds[i], target[i])
#     class_res = mc.compute()
#     func_res = metric_functional(
#         preds_flat, target_flat, num_classes=NUM_CLASSES, average=average, ignore_index=ignore_index
#     )
#     sk_res = sk_fn(target_flat, preds_flat, average=average, zero_division=0)

#     assert torch.allclose(class_res, torch.tensor(sk_res).float())
#     assert torch.allclose(func_res, torch.tensor(sk_res).float())
