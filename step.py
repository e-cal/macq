class NotAnAction(Exception):
    def __init__(self, action, message="Invalid action."):
        self.action = action
        self.message = message
        super().__init__(message)


class Action:
    """Placeholder"""

    pass


class Step:
    """
    A Step object stores the action, fluents being acted on, and state prior
    to the action for a step in a trace.
    """

    def __init__(self, action: Action, fluents: list, state: list):
        """
        Creates a Step object. This stores action, fluents being acted on,
        and state prior to the action.

        Attributes
        ----------
        action : Action
            The action taken in this step.
        fluents : list
            A list of fluents being acted on.
        state : list
            A list of fluents representing the state.
        """
        self.action = action
        self.fluents = fluents
        self.state = state
