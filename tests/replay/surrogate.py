# -*- coding: utf-8 -*-
"""纯标准库 bagged CART 代理模型：预测 + 跨树标准差作为不确定性。

为什么自带代理模型：框架要求零依赖、可在气隙环境运行；小样本（10^2-10^3）
场景下浅层树集成是 §5.1 主力模型的合理简化替身。生产系统应将本模块替换为
LightGBM/XGBoost 适配器——接口（predict / predict_std）保持不变。
"""
from __future__ import annotations
import math
import random


class _Tree:
    """CART 回归树：方差缩减贪心分裂。"""

    def __init__(self, max_depth: int = 4, min_samples_leaf: int = 5,
                 max_features: float = 0.8, rng: random.Random | None = None):
        self.max_depth = max_depth
        self.min_samples_leaf = min_samples_leaf
        self.max_features = max_features
        self.rng = rng or random.Random(0)
        self.root = None

    @staticmethod
    def _sse(y):
        n = len(y)
        m = sum(y) / n
        return sum((v - m) ** 2 for v in y), m

    def fit(self, X, y):
        if not X or len(X) != len(y):
            self.root = {"leaf": True, "value": 0.0, "n": 0}
            return self
        feat_idx = list(range(len(X[0])))
        k = max(1, int(len(feat_idx) * self.max_features))
        self.root = self._build(X, y, feat_idx, k, depth=0)
        return self

    def _build(self, X, y, feat_idx, k, depth):
        sse, mean = self._sse(y)
        n = len(y)
        if depth >= self.max_depth or n < 2 * self.min_samples_leaf or sse < 1e-12:
            return {"leaf": True, "value": mean, "n": n}
        feats = self.rng.sample(feat_idx, k)
        best = None  # (sse_gain, feat, thr)
        for f in feats:
            values = sorted({row[f] for row in X})
            if len(values) < 2:
                continue
            for i in range(len(values) - 1):
                thr = (values[i] + values[i + 1]) / 2.0
                left = [yv for xv, yv in zip(X, y) if xv[f] <= thr]
                right = [yv for xv, yv in zip(X, y) if xv[f] > thr]
                if len(left) < self.min_samples_leaf or len(right) < self.min_samples_leaf:
                    continue
                sl, ml = self._sse(left)
                sr, _ = self._sse(right)
                gain = sse - sl - sr
                if best is None or gain > best[0]:
                    best = (gain, f, thr)
        if best is None or best[0] <= 0:
            return {"leaf": True, "value": mean, "n": n}
        _, f, thr = best
        Xl, yl, Xr, yr = [], [], [], []
        for xv, yv in zip(X, y):
            if xv[f] <= thr:
                Xl.append(xv); yl.append(yv)
            else:
                Xr.append(xv); yr.append(yv)
        return {"leaf": False, "feat": f, "thr": thr,
                "left": self._build(Xl, yl, feat_idx, k, depth + 1),
                "right": self._build(Xr, yr, feat_idx, k, depth + 1)}

    def predict_one(self, x):
        node = self.root
        while not node["leaf"]:
            node = node["left"] if x[node["feat"]] <= node["thr"] else node["right"]
        return node["value"]


class BaggedEnsemble:
    """Bootstrap 聚集的浅层树集成。

    predict      -> 各树均值（点估计）
    predict_std  -> 各树标准差（不确定性，供主动学习采集函数使用）
    """

    def __init__(self, n_trees: int = 40, max_depth: int = 4,
                 min_samples_leaf: int = 5, seed: int = 0):
        self.n_trees = n_trees
        self.max_depth = max_depth
        self.min_samples_leaf = min_samples_leaf
        self.seed = seed
        self.trees = []
        self.X_fit: list = []      # 训练输入留存（供采集函数的外推感知探索项使用）

    def fit(self, X, y):
        self.trees = []
        self.X_fit = [list(x) for x in X]
        if not X or len(X) != len(y):
            return self
        for t in range(self.n_trees):
            rng = random.Random(self.seed * 1000 + t)
            bag = [rng.randrange(len(X)) for _ in range(len(X))]
            tree = _Tree(self.max_depth, self.min_samples_leaf, 0.8, rng)
            tree.fit([X[i] for i in bag], [y[i] for i in bag])
            self.trees.append(tree)
        return self

    def predict(self, x):
        if not self.trees:
            return 0.0
        vals = [t.predict_one(x) for t in self.trees]
        return sum(vals) / len(vals)

    def predict_std(self, x):
        if not self.trees:
            return 0.0
        vals = [t.predict_one(x) for t in self.trees]
        n = len(vals)
        m = sum(vals) / n
        return math.sqrt(sum((v - m) ** 2 for v in vals) / n)
