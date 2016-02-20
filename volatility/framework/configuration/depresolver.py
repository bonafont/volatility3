import logging
from collections import OrderedDict

import volatility.framework as framework
from volatility.framework import validity, interfaces


class DependencyResolver(validity.ValidityRoutines):
    def __init__(self):
        # Maintain a cache of translation layers
        self.configurable_cache = []
        self.provides = {}
        self.providers_cache = sorted(list(self._build_caches(interfaces.configuration.ProviderInterface)),
                                      key = lambda x: -x.priority)

    def _build_caches(self, clazz):
        self.provides = {}
        cache = set()
        for provider in framework.class_subclasses(clazz):
            for k, v in provider.provides.items():
                if not isinstance(v, list):
                    new_v = self.provides.get(k, set())
                    new_v.add(v)
                else:
                    new_v = self.provides.get(k, set()).union(set(v))
                self.provides[k] = new_v
                cache.add(provider)
        return cache

    def satisfies(self, provider, requirement):
        """Takes the requirement (which should always be a TranslationLayerRequirement) and determines if the
           layer_class satisfies it"""
        satisfied = True
        for k, v in requirement.constraints.items():
            if k in provider.provides:
                satisfied = satisfied and bool(self.common_provision(provider.provides[k], v))
        return satisfied

    def common_provision(self, value1, value2):
        """Normalizes individual values down to singleton lists, then tests for overlap between the two lists"""
        if not isinstance(value1, list):
            value1 = [value1]
        if not isinstance(value2, list):
            value2 = [value2]
        set1 = set(value1)
        set2 = set(value2)
        return set1.intersection(set2)

    def validate_dependencies(self, deptree, context, path = None):
        """Takes a dependency tree and attempts to resolve the tree by validating each branch and using the first that successfully validates

            DEPTREE = [ REQUIREMENTS ... ]
            REQUIREMENT = ( NODE | LEAF )
            NODE = req, { candidate : DEPTREE, ... }
            LEAF = req

            @param path: A path to access the deptree's configuration details
        """
        # TODO: Simplify config system access to ensure easier code
        # TODO: Improve logging/output of this code to diagnose errors
        if path is None:
            path = ""
        for node in deptree.children:
            node_path = path + interfaces.configuration.CONFIG_SEPARATOR + node.requirement.name
            if isinstance(node, RequirementTreeChoice) and not node.requirement.optional:
                for provider in node.candidates:
                    if self.validate_dependencies(node.candidates[provider], context, path = node_path):
                        provider.fulfill(context, node.requirement, node_path)
                        break
                else:
                    logging.debug(
                        "Unable to fulfill requirement " + repr(node.requirement) + " - no fulfillable candidates")
                    return False
            try:
                value = context.config[node_path]
                node.requirement.validate(value, context)
            except Exception as e:
                if not node.requirement.optional:
                    logging.debug(
                        "Unable to fulfill non-optional requirement " + repr(node.requirement) +
                        " [" + str(e) + "]")
                    return False
        return True

    def build_tree(self, configurable):
        """Takes a configurable and produces a priority ordered tree of possible solutions to satisfy the various requirements

           @param configurable: A configurable class that requires its dependency tree constructing
           @param path: A path indicating where the configurable resides in the config namespace
           @return deptree: The returned tree should include each of the potential nodes (and requirements, including optional ones) allowing the UI
           to decide the layer build-path and get all the necessary variables from the user for that path.
        """
        self._check_class(configurable, interfaces.configuration.ConfigurableInterface)

        deptree = []

        for subreq in configurable.get_schema():
            # Find all the different ways to fulfill it (recursively)
            # TODO: Ensure no cycles or loops
            if not isinstance(subreq, interfaces.configuration.ConstraintInterface):
                deptree.append(RequirementTreeReq(requirement = subreq))
            else:
                candidates = OrderedDict()
                satisfiable = False
                for potential in self.providers_cache:
                    if self.satisfies(potential, subreq):
                        try:
                            candidate = self.build_tree(potential)
                            candidates[potential] = candidate
                            satisfiable = True
                        except DependencyError:
                            pass
                # Check we've satisfied one of the possibilities, exception if we haven't
                if not satisfiable:
                    raise DependencyError("No solutions to fulfill requirement " + repr(subreq))
                # Construct the appropriate Requirement node
                if candidates:
                    deptree.append(RequirementTreeChoice(requirement = subreq, candidates = candidates))
        return RequirementTreeList(deptree)


class DependencyError(Exception):
    pass


###################################
# Hierarchical Tree Visitor classes
###################################

class TreeVisitor(validity.ValidityRoutines):
    def visit_enter(self, node):
        """Visits a node on entering a branch

           Returns a boolean indicating whether to continue visiting children
        """
        return True

    def visit_leave(self, node):
        """Visits a node on leaving a branch

           Returns a boolean indicating whether to continue visiting further siblings
        """
        return True

    def visit(self, node):
        """Visits the actual leaf node

           Returns boolean indicating whether to continue visiting further siblings
        """
        return True


class ValidateDependenciesVisitor(TreeVisitor):
    def __init__(self, context, node_path = None):
        self.context = self._check_type(context, context.Context)
        if node_path is None:
            node_path = ""
        self.node_path = self._check_type(node_path, str)

    def visit_enter(self, node):
        return True

    def visit_leave(self, node):
        return self.visit(node)

    def visit(self, node):
        try:
            value = self.context.config[self.node_path]
            node.requirement.validate(value, self.context)
        except Exception as e:
            if not node.requirement.optional:
                logging.debug("Unable to fulfill non-optional requirement " +
                              repr(node.requirement) + " [" + str(e) + "]")
                return False
        return True


##########################
# Requirement tree classes
##########################

class RequirementTreeNode(validity.ValidityRoutines):
    @property
    def optional(self):
        """Determines whether the elements within this tree are required for proper operation"""
        return False

    def accept(self, visitor):
        """Takes in a vistor and applies to itself and any child nodes appropriately"""
        return visitor.visit(self)


class RequirementTreeReq(RequirementTreeNode):
    def __init__(self, requirement = None):
        validity.ValidityRoutines.__init__(self)
        self._check_type(requirement, interfaces.configuration.RequirementInterface)
        self.requirement = requirement

    def __repr__(self):
        return "<Leaf: " + repr(self.requirement) + ">"

    @property
    def optional(self):
        return self.requirement.optional


class RequirementTreeChoice(RequirementTreeReq):
    def __init__(self, requirement = None, candidates = None):
        RequirementTreeReq.__init__(self, requirement)
        for k in candidates:
            self._check_class(k, interfaces.configuration.ProviderInterface)
            self._check_type(candidates[k], RequirementTreeList)
        self.candidates = candidates
        if candidates is None:
            self.candidates = OrderedDict()

    def __repr__(self):
        return "<Node: " + repr(self.requirement) + " Candidates: " + repr(self.candidates) + ">"

    def accept(self, visitor):
        if (visitor.visit_enter(self)):
            for candidate in self.candidates:
                if not self.candidates[candidate].apply(visitor):
                    break

        return visitor.visit_leave(self)


class RequirementTreeList(RequirementTreeNode):
    def __init__(self, children = None):
        self._check_type(children, list)
        for child in children:
            self._check_type(child, RequirementTreeNode)
        self.children = children

    @property
    def optional(self):
        return False

    def accept(self, visitor):
        if (visitor.visit_enter(self)):
            for candidate in self.children:
                if not candidate.apply(visitor):
                    break

        return visitor.visit_leave(self)
