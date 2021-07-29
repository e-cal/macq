from collections import defaultdict, Counter
from dataclasses import dataclass
from typing import Set, List, Dict, Tuple, Hashable
from nnf import NNF, Var, And, Or, false as nnffalse
from pysat.examples.rc2 import RC2
from pysat.formula import WCNF
from . import LearnedAction, Model
from .exceptions import (
    ConstraintContradiction,
    IncompatibleObservationToken,
    InconsistentConstraintWeights,
    InvalidMaxSATModel,
)
from ..observation import PartialObservation as Observation
from ..trace import ObservationLists, Fluent, Action  # Action only used for typing
from ..utils.pysat import to_wcnf


@dataclass
class Relation:
    name: str
    types: list

    def var(self):
        return f"{self.name} {' '.join(list(self.types))}"

    def __hash__(self):
        return hash(self.var())


@dataclass
class ARMSConstraints:
    action: List[Or[Var]]
    info: List[Or[Var]]
    info3: Dict[Or[Var], int]
    plan: Dict[Or[Var], int]


class ARMS:
    """ARMS model extraction method.

    Extracts a Model from state observations using the ARMS technique. Fluents
    are retrieved from the initial state. Actions are learned using the
    algorithm.
    """

    class InvalidThreshold(Exception):
        def __init__(self, threshold):
            super().__init__(
                f"Invalid threshold value: {threshold}. Threshold must be a float between 0-1 (inclusive)."
            )

    def __new__(
        cls,
        obs_lists: ObservationLists,
        debug: bool,
        upper_bound: int,
        min_support: int = 2,
        action_weight: int = 110,
        info_weight: int = 100,
        threshold: float = 0.6,
        info3_default: int = 30,
        plan_default: int = 30,
    ):
        """
        Arguments:
            obs_lists (ObservationLists):
                The observations to extract the model from.
            upper_bound (int):
                The upper bound for the maximum size of an action's preconditions and
                add/delete lists. Determines when an action schemata is fully learned.
            min_support (int):
                The minimum support count for an action pair to be considered frequent.
            action_weight (int):
                The constant weight W_A(a) to assign to each action constraint.
            info_weight (int):
                The constant weight W_I(r) to assign to each information constraint.
            threshold (float):
                (0-1) The probability threshold θ to determine if an I3/plan constraint
                is weighted by its probability or set to a default value.
            info3_default (int):
                The default weight for I3 constraints with probability below the threshold.
            plan_default (int):
                The default weight for plan constraints with probability below the threshold.
        """
        if obs_lists.type is not Observation:
            raise IncompatibleObservationToken(obs_lists.type, ARMS)

        if not (threshold >= 0 and threshold <= 1):
            raise ARMS.InvalidThreshold(threshold)

        # get fluents from initial state
        fluents = obs_lists.get_fluents()
        # call algorithm to get actions
        actions = ARMS._arms(
            obs_lists,
            upper_bound,
            fluents,
            min_support,
            action_weight,
            info_weight,
            threshold,
            info3_default,
            plan_default,
            debug,
        )
        return Model(fluents, actions)

    @staticmethod
    def _arms(
        obs_lists: ObservationLists,
        upper_bound: int,
        fluents: Set[Fluent],
        min_support: int,
        action_weight: int,
        info_weight: int,
        threshold: float,
        info3_default: int,
        plan_default: int,
        debug: bool,
    ) -> Set[LearnedAction]:
        """The main driver for the ARMS algorithm."""
        learned_actions = set()

        connected_actions, action_map = ARMS._step1(obs_lists, debug)

        action_map_rev: Dict[LearnedAction, List[Action]] = defaultdict(list)
        for obs_action, learned_action in action_map.items():
            action_map_rev[learned_action].append(obs_action)

        while action_map_rev:
            constraints, relations = ARMS._step2(
                obs_lists, connected_actions, action_map, fluents, min_support, debug
            )

            max_sat, decode = ARMS._step3(
                constraints,
                action_weight,
                info_weight,
                threshold,
                info3_default,
                plan_default,
                debug,
            )

            model = ARMS._step4(max_sat, decode, debug)

            # actions mutated in place (don't need to return)
            ARMS._step5(
                model, list(action_map_rev.keys()), list(relations.values()), debug
            )

            setA = set()
            for action in action_map_rev.keys():
                if debug:
                    ARMS.debug(action=action)
                if (
                    max([len(action.precond), len(action.add), len(action.delete)])
                    >= upper_bound
                ):
                    if debug:
                        print(
                            f"Action schemata for {action.details()} has been fully learned."
                        )
                    setA.add(action)

            for action in setA:
                action_keys = action_map_rev[action]
                for obs_action in action_keys:
                    del action_map[obs_action]
                del action_map_rev[action]
                del connected_actions[action]
                action_keys = [
                    a1 for a1 in connected_actions if action in connected_actions[a1]
                ]
                for a1 in action_keys:
                    del connected_actions[a1][action]
                learned_actions.add(action)

        # TODO
        # return set(learned_actions.values())
        return set()

    @staticmethod
    def _step1(
        obs_lists: ObservationLists, debug: bool
    ) -> Tuple[
        Dict[LearnedAction, Dict[LearnedAction, Set[str]]],
        Dict[Action, LearnedAction],
    ]:
        """Substitute instantiated objects in each action instance with the object type."""

        actions: List[LearnedAction] = []
        learned_actions = {}
        for obs_action in obs_lists.get_actions():
            # We don't support objects with multiple types right now, so no
            # multiple type clauses need to be generated.

            # Create LearnedActions for each action, replacing instantiated
            # objects with the object type.
            types = {obj.obj_type for obj in obs_action.obj_params}
            action = LearnedAction(obs_action.name, types)
            actions.append(action)
            learned_actions[obs_action] = action

        connected_actions: Dict[LearnedAction, Dict[LearnedAction, Set[str]]] = {}
        for i, a1 in enumerate(actions):
            connected_actions[a1] = {}
            for a2 in actions[i:]:  # includes connecting with self
                intersection = a1.obj_params.intersection(a2.obj_params)
                if intersection:
                    connected_actions[a1][a2] = intersection

        return connected_actions, learned_actions

    @staticmethod
    def _step2(
        obs_lists: ObservationLists,
        connected_actions: Dict[LearnedAction, Dict[LearnedAction, Set]],
        action_map: Dict[Action, LearnedAction],
        fluents: Set[Fluent],
        min_support: int,
        debug: bool,
    ) -> Tuple[ARMSConstraints, Dict[Fluent, Relation]]:
        """Generate action constraints, information constraints, and plan constraints."""

        # Map fluents to relations
        # relations are fluents but with instantiated objects replaced by the object type
        relations: Dict[Fluent, Relation] = dict(
            map(
                lambda f: (
                    f,
                    Relation(
                        f.name,  # the fluent name
                        [obj.obj_type for obj in f.objects],  # the object types
                    ),
                ),
                fluents,
            )
        )

        action_constraints = ARMS._step2A(
            connected_actions, set(relations.values()), debug
        )
        info_constraints, info_support_counts = ARMS._step2I(
            obs_lists, relations, action_map, debug
        )

        plan_constraints = ARMS._step2P(
            obs_lists,
            connected_actions,
            action_map,
            set(relations.values()),
            min_support,
        )

        return (
            ARMSConstraints(
                action_constraints,
                info_constraints,
                info_support_counts,
                plan_constraints,
            ),
            relations,
        )

    @staticmethod
    def _step2A(
        connected_actions: Dict[LearnedAction, Dict[LearnedAction, Set]],
        relations: Set[Relation],
        debug: bool,
    ) -> List[Or[Var]]:

        if debug:
            print("Building action constraints...\n")

        def implication(a: Var, b: Var):
            return Or([a.negate(), b])

        constraints: List[Or[Var]] = []
        actions = set(connected_actions.keys())
        for action in actions:
            for relation in relations:
                # A relation is relevant to an action if they share parameter types
                if relation.types and set(relation.types).issubset(action.obj_params):
                    if debug:
                        print(
                            f'relation ({relation.var()}) is relevant to action "{action.details()}"\n'
                            "A1:\n"
                            f"  {relation.var()} ∈ add ⇒ {relation.var()} ∉ pre\n"
                            f"  {relation.var()} ∈ pre ⇒ {relation.var()} ∉ add\n"
                            "A2:\n"
                            f"  {relation.var()} ∈ del ⇒ {relation.var()} ∈ pre\n"
                        )
                    # A1
                    # relation in action.add => relation not in action.precond
                    # relation in action.precond => relation not in action.add

                    # _ is used to unambiguously mark split locations for parsing later.
                    constraints.append(
                        implication(
                            Var(f"{relation.var()}_in_add_{action.details()}"),
                            Var(f"{relation.var()}_in_pre_{action.details()}").negate(),
                        )
                    )
                    constraints.append(
                        implication(
                            Var(f"{relation.var()}_in_pre_{action.details()}"),
                            Var(f"{relation.var()}_in_add_{action.details()}").negate(),
                        )
                    )

                    # A2
                    # relation in action.del => relation in action.precond
                    constraints.append(
                        implication(
                            Var(f"{relation.var()}_in_del_{action.details()}"),
                            Var(f"{relation.var()}_in_pre_{action.details()}"),
                        )
                    )

        return constraints

    @staticmethod
    def _step2I(
        obs_lists: ObservationLists,
        relations: Dict[Fluent, Relation],
        actions: Dict[Action, LearnedAction],
        debug: bool,
    ) -> Tuple[List[Or[Var]], Dict[Or[Var], int]]:
        constraints: List[Or[Var]] = []
        support_counts: Dict[Or[Var], int] = defaultdict(int)
        obs_list: List[Observation]
        for obs_list_i, obs_list in enumerate(obs_lists):
            for i, obs in enumerate(obs_list):
                if obs.state is not None and i > 0:
                    if debug:
                        print(
                            f"State {i+1} of observation list {obs_list_i+1} has information."
                        )
                    for fluent, val in obs.state.items():
                        # Information constraints only apply to true relations
                        if val:
                            # I1
                            # relation in the add list of an action <= n (i-1)
                            i1: List[Var] = []
                            for obs_i in obs_list[: i - 1]:
                                # action will never be None if it's in actions,
                                # but the condition is needed to make linting happy
                                if obs_i.action in actions and obs_i.action is not None:
                                    ai = actions[obs_i.action]
                                    i1.append(
                                        Var(
                                            f"{relations[fluent].var()}_in_add_{ai.details()}"
                                        )
                                    )

                            # I2
                            # relation not in del list of action n (i-1)
                            i2 = None
                            if obs_list[i - 1].action in actions:
                                i2 = Var(
                                    f"{relations[fluent].var()}_in_del_{actions[obs_list[i-1].action].details()}"
                                ).negate()

                            if i1:
                                constraints.append(Or(i1))
                            if i2:
                                constraints.append(Or([i2]))

                            # I3
                            # count occurences
                            if i < len(obs_list) - 1 and obs.action in actions:
                                # corresponding constraint is related to the current action's precondition list
                                support_counts[
                                    Or(
                                        [
                                            Var(
                                                f"{relations[fluent].var()}_in_pre_{actions[obs.action].details()}"
                                            )
                                        ]
                                    )
                                ] += 1
                            elif obs_list[i - 1].action in actions:
                                # corresponding constraint is related to the previous action's add list
                                support_counts[
                                    Or(
                                        [
                                            Var(
                                                f"{relations[fluent].var()}_in_add_{actions[obs_list[i-1].action].details()}"
                                            )
                                        ]
                                    )
                                ] += 1

        return constraints, support_counts

    @staticmethod
    def _apriori(
        action_lists: List[List[LearnedAction]], minsup: int
    ) -> Dict[Tuple[LearnedAction, LearnedAction], int]:
        """An implementation of the Apriori algorithm to find frequent ordered pairs of actions."""
        counts = Counter(
            [action for action_list in action_lists for action in action_list]
        )
        # L1 = {actions that appear >minsup}
        L1 = set(
            frozenset([action])
            for action in filter(lambda k: counts[k] >= minsup, counts.keys())
        )  # large 1-itemsets

        # Only going up to L2, so no loop or generalized algorithm needed
        # apriori-gen step
        C2 = set([i.union(j) for i in L1 for j in L1 if len(i.union(j)) == 2])
        # Since L1 contains 1-itemsets where each item is frequent, C2 can
        # only contain valid sets and pruning is not required

        # Get all possible ordered action pairs
        C2_ordered = set()
        for pair in C2:
            pair = list(pair)
            C2_ordered.add((pair[0], pair[1]))
            C2_ordered.add((pair[1], pair[0]))

        # Count pair occurences and generate L2
        frequent_pairs = {}
        for ai, aj in C2_ordered:
            count = 0
            for action_list in action_lists:
                a1_indecies = [i for i, e in enumerate(action_list) if e == ai]
                if a1_indecies:
                    for i in a1_indecies:
                        if aj in action_list[i + 1 :]:
                            count += 1
            if count >= minsup:
                frequent_pairs[(ai, aj)] = count

        return frequent_pairs

    @staticmethod
    def _step2P(
        obs_lists: ObservationLists,
        connected_actions: Dict[LearnedAction, Dict[LearnedAction, Set]],
        action_map: Dict[Action, LearnedAction],
        relations: Set[Relation],
        min_support: int,
    ) -> Dict[Or[Var], int]:
        # ) -> Dict[And[Or[Var]], int]:
        frequent_pairs = ARMS._apriori(
            [
                [
                    action_map[obs.action]
                    for obs in obs_list
                    if obs.action is not None and obs.action in action_map
                ]
                for obs_list in obs_lists
            ],
            min_support,
        )

        # constraints: Dict[And[Or[Var]], int] = {}
        constraints: Dict[Or[Var], int] = {}
        for ai, aj in frequent_pairs.keys():
            connectors = set()
            # get list of relevant relations from connected_actions
            if ai in connected_actions and aj in connected_actions[ai]:
                connectors.update(connected_actions[ai][aj])
            if aj in connected_actions and ai in connected_actions[aj]:
                connectors.update(connected_actions[aj][ai])

            # if the actions are not related they are not a valid pair for a plan constraint
            if not connectors:
                continue

            # for each relation, save constraint
            relevant_relations = {p for p in relations if connectors.issubset(p.types)}
            # relation_constraints: List[Or[And[Var]]] = []
            relation_constraints: List[Var] = []
            for relation in relevant_relations:
                """
                ∃p(
                  (p∈ (pre_i ∩ pre_j) ∧ p∉ (del_i)) ∨
                  (p∈ (add_i ∩ pre_j)) ∨
                  (p∈ (del_i ∩ add_j))
                )
                where p is a relevant relation.
                """
                # Phi = Or(
                #     [
                #         And(
                #             [
                #                 Var(f"{relation.var()}_in_pre_{ai.details()}"),
                #                 Var(f"{relation.var()}_in_pre_{aj.details()}"),
                #                 Var(f"{relation.var()}_in_del_{ai.details()}").negate(),
                #             ]
                #         ),
                #         And(
                #             [
                #                 Var(f"{relation.var()}_in_add_{ai.details()}"),
                #                 Var(f"{relation.var()}_in_pre_{aj.details()}"),
                #             ]
                #         ),
                #         And(
                #             [
                #                 Var(f"{relation.var()}_in_del_{ai.details()}"),
                #                 Var(f"{relation.var()}_in_add_{aj.details()}"),
                #             ]
                #         ),
                #     ]
                # )
                relation_constraints.append(
                    Var(f"{relation.var()}_relevant_{ai.details()}_{aj.details()}")
                )
            constraints[Or(relation_constraints)] = frequent_pairs[(ai, aj)]

        return constraints

    @staticmethod
    def _step3(
        constraints: ARMSConstraints,
        action_weight: int,
        info_weight: int,
        threshold: float,
        info3_default: int,
        plan_default: int,
    ) -> Tuple[WCNF, Dict[int, Hashable]]:
        """Construct the weighted MAX-SAT problem."""

        action_weights = [action_weight] * len(constraints.action)
        info_weights = [info_weight] * len(constraints.info)
        info3_weights = ARMS._calculate_support_rates(
            list(constraints.info3.values()), threshold, info3_default
        )
        plan_weights = ARMS._calculate_support_rates(
            list(constraints.plan.values()), threshold, plan_default
        )
        all_weights = action_weights + info_weights + info3_weights + plan_weights

        info3_constraints = list(constraints.info3.keys())
        plan_constraints = list(constraints.plan.keys())
        all_constraints = (
            constraints.action + constraints.info + info3_constraints + plan_constraints
        )

        constraints_w_weights: Dict[Or[Var], int] = {}
        for constraint, weight in zip(all_constraints, all_weights):
            if constraint == nnffalse:
                continue
            if constraint not in constraints_w_weights:
                constraints_w_weights[constraint] = weight
            elif weight != constraints_w_weights[constraint]:
                raise InconsistentConstraintWeights(
                    constraint, weight, constraints_w_weights[constraint]
                )

        problem: And[Or[Var]] = And(list(constraints_w_weights.keys()))
        weights = list(constraints_w_weights.values())

        wcnf, decode = to_wcnf(problem, weights)
        return wcnf, decode

    @staticmethod
    def _calculate_support_rates(
        support_counts: List[int], threshold: float, default: int
    ) -> List[int]:
        # NOTE:
        # In the paper, Z_Σ_P (denominator of the support rate formula) is
        # defined as the "total pairs" in the set of plans. However, in the
        # examples it appears that they use the max support count as the
        # denominator. My best interpretation is then to use the max support
        # count as the denominator to calculate the support rate.

        z_sigma_p = max(support_counts)

        def get_support_rate(count):
            probability = count / z_sigma_p
            return probability * 100 if probability > threshold else default

        return list(map(get_support_rate, support_counts))

    @staticmethod
    def _step4(max_sat: WCNF, decode: Dict[int, Hashable]) -> Dict[Hashable, bool]:
        solver = RC2(max_sat)
        solver.compute()
        encoded_model = solver.model
        if not isinstance(encoded_model, list):
            raise InvalidMaxSATModel(encoded_model)

        # decode the model (back to nnf vars)
        model: Dict[Hashable, bool] = {
            decode[abs(clause)]: clause > 0 for clause in encoded_model
        }

        return model

    @staticmethod
    def _step5(
        model: Dict[Hashable, bool],
        actions: List[LearnedAction],
        relations: List[Relation],
    ):
        action_map = {a.details(): a for a in actions}
        relation_map = {p.var(): p for p in relations}

        for constraint, val in list(model.items())[:50]:
            constraint = str(constraint).split("_")
            fluent = relation_map[constraint[0]]
            relation = constraint[0]
            ctype = constraint[1]  # constraint type
            if ctype == "in":
                effect = constraint[2]
                action = action_map[constraint[3]]
                if val:
                    action_update = (
                        action.update_precond
                        if effect == "pre"
                        else action.update_add
                        if effect == "add"
                        else action.update_delete
                    )
                    # action_update({str(fluent)})
                    action_update({relation})
                else:
                    action_effect = (
                        action.precond
                        if effect == "pre"
                        else action.add
                        if effect == "add"
                        else action.delete
                    )
                    if fluent in action_effect:
                        # TODO: determine if this is an error, or if it just
                        # means the effect should be removed (due to more info
                        # from later iterations)
                        raise ConstraintContradiction(fluent, effect, action)
            else:
                # plan constraint
                # doesn't directly affect actions
                a1 = constraint[2]
                a2 = constraint[3]

    @staticmethod
    def debug(action=None):
        if action:
            print()
            print(action.details())
            print("precond:", action.precond)
            print("add:", action.add)
            print("delete:", action.delete)
