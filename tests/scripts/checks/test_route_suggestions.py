"""`make route-suggestions` compares each question's mark with where the router sends it (a fake classifier here: no ML service)."""
import pytest

from app.ports.outbound.classification import Labelled, route_classifier
from scripts.checks import route_suggestions


class _Classifier:
    def __init__(self, labelled: Labelled):
        self.labelled = labelled

    async def choose(self, question, text):
        return self.labelled


@pytest.mark.parametrize(
    "labelled, entry, wrong",
    [
        (Labelled("labelled", label="reasoning"), {"text": "q", "route": "strong"}, 0),
        (Labelled("labelled", label="lookup"), {"text": "q"}, 0),
        (Labelled("labelled", label="analysis"), {"text": "q"}, 1),  # routed, and not marked
        (Labelled("labelled", label="lookup"), {"text": "q", "route": "medium"}, 1),
        (Labelled("timeout"), {"text": "q"}, 1),  # rules alone would have agreed: no answer is not a pass
    ],
)
async def test_a_mark_that_disagrees_or_cannot_be_judged_is_counted(labelled, entry, wrong):
    with route_classifier.overriding(_Classifier(labelled)):
        assert await route_suggestions.check({"ops": [entry]}) == wrong
