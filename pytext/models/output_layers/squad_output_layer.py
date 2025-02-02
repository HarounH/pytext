#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved

from typing import Any, Dict, Iterable, List, Optional, Tuple

import torch
import torch.nn.functional as F
from pytext.config.component import create_loss
from pytext.fields import FieldMeta
from pytext.loss import CrossEntropyLoss, Loss
from pytext.models.output_layers import OutputLayerBase


class SquadOutputLayer(OutputLayerBase):
    class Config(OutputLayerBase.Config):
        loss: CrossEntropyLoss.Config = CrossEntropyLoss.Config()
        ignore_impossible: bool = True
        pos_loss_weight: float = 0.5
        has_answer_loss_weight: float = 0.5
        false_label: str = "False"
        max_answer_len: int = 30

    @classmethod
    def from_config(
        cls,
        config,
        metadata: Optional[FieldMeta] = None,
        labels: Optional[Iterable[str]] = None,
    ):
        return cls(
            loss_fn=create_loss(config.loss, ignore_index=-100),
            ignore_impossible=config.ignore_impossible,
            pos_loss_weight=config.pos_loss_weight,
            has_answer_loss_weight=config.has_answer_loss_weight,
            has_answer_labels=labels,
            false_label=config.false_label,
            max_answer_len=config.max_answer_len,
        )

    def __init__(
        self,
        loss_fn: Loss,
        ignore_impossible: bool = Config.ignore_impossible,
        pos_loss_weight: float = Config.pos_loss_weight,
        has_answer_loss_weight: float = Config.has_answer_loss_weight,
        has_answer_labels: Iterable[str] = ("False", "True"),
        false_label: str = Config.false_label,
        max_answer_len: int = Config.max_answer_len,
    ) -> None:
        super().__init__(loss_fn=loss_fn)
        self.pos_loss_weight = pos_loss_weight
        self.has_answer_loss_weight = has_answer_loss_weight
        self.has_answer_labels = has_answer_labels
        self.ignore_impossible = ignore_impossible
        self.max_answer_len = max_answer_len
        if not ignore_impossible:
            self.false_idx = 1 if has_answer_labels[1] == false_label else 0
            self.true_idx = 1 - self.false_idx

    def get_position_preds(
        self,
        start_pos_logits: torch.Tensor,
        end_pos_logits: torch.Tensor,
        max_span_length: int,
    ):
        # the following is to enforce end_pos > start_pos.  We create a matrix
        # of start_position X end_position, fill it with the sum logits,
        # then mask it to be upper-triangular
        # e.g. start_pos_logits = [1, 3, 0, 5, 2]
        #      end_pos_logits = [2, 4, 6, 3, 5]
        # The max indices should be (3,4) with values (5,5).  (5,6) would have a
        # higher score, but end_pos would be before start, so it's not feasible
        #
        # To calculate this, first create a matrix with i,j entry containing
        # start_pos_logits[i] + end_pos_logits[j]
        #                   = [[3, 5, 7, 4, 6],
        #                     [4, 7, 9, 6, 8],
        #                     [2, 4, 6, 3, 5],
        #                     [7, 9, 11, 8, 10],
        #                     [4, 6, 8, 5, 7]]
        # Then mask it to be upper-triagular:
        #  logit_sum_matrix = [[3, 5, 7, 4, 6],
        #                     [0, 7, 9, 6, 8],
        #                     [0, 0, 6, 3, 5],
        #                     [0, 0, 0, 8, 10],
        #                     [0, 0, 0, 0, 7]]
        # Then we use argmax to retrieve the indices of the max value.
        size = start_pos_logits.size() + (start_pos_logits.size()[-1],)
        start_pos_logits = start_pos_logits.unsqueeze(-1).expand(size) + 10
        end_pos_logits = (
            end_pos_logits.unsqueeze(-1).expand(size).transpose(-2, -1) + 10
        )
        logit_sum_matrix = (start_pos_logits + end_pos_logits).triu()
        for i in range(logit_sum_matrix.size()[1]):
            logit_sum_matrix[:, i, i + max_span_length :] = 0
        vals, ids = logit_sum_matrix.max(-1)
        _, start_position = vals.max(-1)
        end_position = ids.gather(-1, start_position.unsqueeze(-1)).squeeze(-1)

        return start_position, end_position

    def get_pred(
        self,
        logits: torch.Tensor,
        targets: torch.Tensor,
        contexts: Dict[str, List[Any]],
    ) -> Tuple[Tuple[torch.Tensor, torch.Tensor], Tuple[torch.Tensor, torch.Tensor]]:
        start_pos_logits, end_pos_logits, has_answer_logits = logits
        start_pos_preds, end_pos_preds = self.get_position_preds(
            start_pos_logits, end_pos_logits, self.max_answer_len
        )
        has_answer_preds = has_answer_logits.float().argmax(-1)
        has_answer_scores = torch.zeros(has_answer_logits.size())
        if not self.ignore_impossible:
            has_answer_scores = F.softmax(has_answer_logits, 1)

        # Compute the logit of the corresponding to start and end positions.
        start_pos_scores = (
            F.softmax(start_pos_logits, 1)
            .gather(1, start_pos_preds.view(-1, 1))
            .squeeze(-1)
        )
        end_pos_scores = (
            F.softmax(end_pos_logits, 1)
            .gather(1, end_pos_preds.view(-1, 1))
            .squeeze(-1)
        )

        return (
            (start_pos_preds, end_pos_preds, has_answer_preds),
            (start_pos_scores, end_pos_scores, has_answer_scores),
        )

    def get_loss(
        self,
        logits: Tuple[torch.Tensor, torch.Tensor],
        targets: Tuple[torch.Tensor, torch.Tensor, torch.Tensor],
        contexts: Dict[str, Any] = None,
        *args,
        **kwargs,
    ) -> torch.Tensor:
        """Compute and return the loss given logits and targets.

        Args:
            logit (torch.Tensor): Logits returned :class:`~pytext.models.Model`.
            target (torch.Tensor): True label/target to compute loss against.
            context (Optional[Dict[str, Any]]): Context is a dictionary of items
                that's passed as additional metadata by the
                :class:`~pytext.data.DataHandler`. Defaults to None.=

        Returns:
            torch.Tensor: Model loss.

        """
        start_pos_logit, end_pos_logit, has_answer_logit = logits
        start_pos_target, end_pos_target, has_answer_target = targets

        num_answers = start_pos_target.size()[-1]
        if num_answers == 0:
            start_loss = torch.tensor(
                0.0, dtype=torch.float, requires_grad=True
            ).type_as(end_pos_logit)
            end_loss = torch.tensor(0.0, dtype=torch.float, requires_grad=True).type_as(
                end_pos_logit
            )
        else:
            start_loss = self.loss_fn(
                start_pos_logit.repeat((num_answers, 1)),
                start_pos_target.transpose(1, 0).flatten(),
                reduce=False,
            )
            end_loss = self.loss_fn(
                end_pos_logit.repeat((num_answers, 1)),
                end_pos_target.transpose(1, 0).flatten(),
                reduce=False,
            )
        loss = (start_loss + end_loss).mean()
        if not self.ignore_impossible:
            has_answer_mask = (
                has_answer_target.repeat((num_answers,)) == self.true_idx
            ).float()
            position_loss = (has_answer_mask * (start_loss + end_loss)).mean()
            has_answer_loss = self.loss_fn(has_answer_logit, has_answer_target)
            loss = (
                self.has_answer_loss_weight * has_answer_loss
                + self.pos_loss_weight * position_loss
            )
        return loss
