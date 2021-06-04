from typing import Set
from ..trace import Action, Fluent


class ModelAction:
    def __init__(self, action: Action):
        self.name = action.name
        self.obj_params = action.obj_params
        self.cost = action.cost
        self.precond = set()
        self.add = set()
        self.delete = set()

    def __str__(self):
        string = f"{self.name} {' '.join(map(str, self.obj_params))}"
        return string

    def __eq__(self, other):
        if not isinstance(other, ModelAction):
            return False
        return self.name == other.name and self.obj_params == other.obj_params

    def __hash__(self):
        # Order of obj_params is important!
        return hash(str(self))

    def update_precond(self, fluents: Set[Fluent]):
        """Adds preconditions to the action.

        Args:
            fluents (set):
                The set of fluents to be added to the action's preconditions.
        """
        self.precond.update(fluents)

    def update_add(self, fluents: Set[Fluent]):
        """Adds add effects to the action.

        Args:
            fluents (set):
                The set of fluents to be added to the action's add effects.
        """
        self.add.update(fluents)

    def update_delete(self, fluents: Set[Fluent]):
        """Adds delete effects to the action.

        Args:
            fluents (set):
                The set of fluents to be added to the action's delete effects.
        """
        self.delete.update(fluents)
