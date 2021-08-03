from ..utils import PercentError
from ..trace import Step, Fluent, State
from ..trace import PartialState
from . import PartialObservation
from typing import Set
from logging import warning
import random


class NoisyPartialObservation(PartialObservation):
    """The Noisy Partial Observability Token.

    The noisy partial observability token stores the step where some of the values of
    the fluents in the step's state are incorrect (noisy) or unknown (partial). Inherits
    the PartialObservation token class, as this token just adds noisiness. 

    This token can also be used to create states that are noisy but fully observable -- just
    set percent_missing to 0.
    """

    def __init__(
        self, step: Step, percent_missing: float = 0, hide: Set[Fluent] = None, percent_noisy: float = 0, replace_noisy: bool = False):
        """
        Creates an NoisyPartialObservation object, storing the step.

        Args:
            step (Step):
                The step associated with this observation.
            percent_missing (float):
                The percentage of fluents to randomly hide in the observation.
            hide (Set[Fluent]):
                The set of fluents to explicitly hide in the observation.
            percent_noisy (float):
                The percentage of fluents to randomly make noisy in the observation.
            replace_noisy (bool):
                An optional style of noisiness where instead of directly flipping the values of noisy propositions, the values are 
                randomly replaced with the values of other propositions.
        """

        super().__init__(step=step, percent_missing=percent_missing, hide=hide, )

        if percent_noisy < 1:
            step = self.random_noisy_subset(percent_noisy, replace_noisy)

        self.state = None if percent_missing == 1 else step.state.clone()
        self.action = None if step.action is None else step.action.clone()

    def random_noisy_subset(self, percent_noisy: float, replace_noisy: bool):
        # using the updated state after any fluent "hiding" took place (from the partial obs. setting);
        # exclude any hidden fluents. (we want to keep hidden fluents and noisy fluents separate).
        fluents = {}
        for f in self.state:
            if self.state[f] is not None:
                fluents[f] = self.state[f] 
        new_fluents = {}
        noisy_f = self.extract_fluent_subset(fluents, percent_noisy)

        if not replace_noisy:
            for f in fluents:
                new_fluents[f] = not self.state[f] if f in noisy_f else self.state[f]
        else:
            for f in fluents:
                new_fluents[f] = self.state[random.choice(list(fluents.keys()))] if f in noisy_f else self.state[f]
        return Step(State(new_fluents), self.action, self.index)


