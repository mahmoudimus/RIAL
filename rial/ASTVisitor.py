from llvmlite import ir
from llvmlite.ir import PointerType, GlobalVariable, FormattedConstant

from rial.LLVMGen import LLVMGen
from rial.ParserState import ParserState
from rial.builtin_type_to_llvm_mapper import map_llvm_to_type, NULL
from rial.concept.metadata_token import MetadataToken
from rial.concept.name_mangler import mangle_function_name
from rial.concept.parser import Interpreter, Tree, Token, Discard
from rial.log import log_fail
from rial.metadata.RIALFunction import RIALFunction
from rial.rial_types.RIALAccessModifier import RIALAccessModifier
from rial.type_casting import get_casting_function


class ASTVisitor(Interpreter):
    llvmgen: LLVMGen

    def __init__(self):
        super().__init__()
        self.llvmgen = LLVMGen()

    def transform_helper(self, node):
        if isinstance(node, Tree):
            node = self.visit(node)

        return node

    def smaller_than(self, tree: Tree):
        nodes = tree.children
        left = self.transform_helper(nodes[0])
        right = self.transform_helper(nodes[2])

        return self.llvmgen.gen_comparison('<', left, right)

    def bigger_than(self, tree: Tree):
        nodes = tree.children
        left = self.transform_helper(nodes[0])
        right = self.transform_helper(nodes[2])

        return self.llvmgen.gen_comparison('>', left, right)

    def bigger_equal(self, tree: Tree):
        nodes = tree.children
        left = self.transform_helper(nodes[0])
        right = self.transform_helper(nodes[2])

        return self.llvmgen.gen_comparison('>=', left, right)

    def smaller_equal(self, tree: Tree):
        nodes = tree.children
        left = self.transform_helper(nodes[0])
        right = self.transform_helper(nodes[2])

        return self.llvmgen.gen_comparison('<=', left, right)

    def equal(self, tree: Tree):
        nodes = tree.children
        left = self.transform_helper(nodes[0])
        right = self.transform_helper(nodes[2])

        return self.llvmgen.gen_comparison('==', left, right)

    def var(self, tree: Tree):
        if isinstance(tree, Tree):
            nodes = tree.children
        else:
            nodes = [tree]
        token: Token

        if isinstance(nodes[0], Tree):
            identifier = '.'.join([node.value for node in nodes[0].children])
            token = nodes[0].children[0]
        elif isinstance(nodes, list):
            identifier = '.'.join([node.value for node in nodes])
            token = nodes[0]
        else:
            identifier = nodes[0].value
            token = nodes[0]

        va = self.llvmgen.get_var(identifier)

        if va is None:
            log_fail(f"{ParserState.module().name}[{token.line}:{token.column}] Error 0001")
            log_fail(f"Could not find identifier {identifier}")

        return va

    def math(self, tree: Tree):
        nodes = tree.children

        left = self.transform_helper(nodes[0])
        right = self.transform_helper(nodes[2])

        op = nodes[1].type

        if op == "PLUS":
            return self.llvmgen.gen_addition(left, right)
        elif op == "MINUS":
            return self.llvmgen.gen_subtraction(left, right)
        elif op == "MUL":
            return self.llvmgen.gen_multiplication(left, right)
        elif op == "DIV":
            return self.llvmgen.gen_division(left, right)

    def variable_assignment(self, tree: Tree):
        nodes = tree.children

        variable = self.var(nodes[0])
        value = self.transform_helper(nodes[2])

        if isinstance(value, RIALFunction):
            value = ir.PointerType(value)

        # TODO: Check if types are matching based on both LLVM value and inferred and metadata RIAL type
        return self.llvmgen.assign_to_variable(variable, value)

    def variable_decl(self, tree: Tree):
        nodes = tree.children
        identifier = nodes[0].value
        value = self.transform_helper(nodes[2])

        if isinstance(value, RIALFunction):
            value = ir.PointerType(value)

        return self.llvmgen.declare_variable(identifier, value)

    def global_variable_decl(self, tree: Tree):
        nodes = tree.children[0].children
        access_modifier = RIALAccessModifier.PRIVATE
        variable_name = nodes[0].value
        variable_value = nodes[2]
        variable_type = hasattr(variable_value, 'type') and variable_value.type or None

        if not isinstance(variable_value, ir.Constant) or isinstance(variable_value, FormattedConstant):
            if isinstance(variable_value, GlobalVariable) and variable_value.global_constant:
                variable_value = variable_value.initializer
                variable_type = variable_value.initializer.type
            else:
                raise PermissionError("Global variables can only be of constant types right now")

        glob = self.llvmgen.gen_global(variable_name, variable_value, variable_type, access_modifier,
                                       access_modifier.get_linkage(),
                                       False)

        return glob

    def cast(self, tree: Tree):
        nodes = tree.children
        ty = ParserState.map_type_to_llvm(nodes[0])
        value = self.llvmgen.gen_load_if_necessary(self.transform_helper(nodes[1]))
        cast_function = get_casting_function(value.type, ty)

        if hasattr(self.llvmgen.builder, cast_function):
            return getattr(self.llvmgen.builder, cast_function)(value, ty)

        log_fail(f"Invalid cast from {value.type} to {ty}")

        raise Discard()

    def continue_rule(self, tree: Tree):
        if self.llvmgen.conditional_block is None:
            log_fail(f"'continue' outside of loop")
            return None

        return self.llvmgen.create_jump(self.llvmgen.conditional_block)

    def break_rule(self, tree: Tree):
        if self.llvmgen.end_block is None:
            log_fail(f"'break' outside of loop or conditional block")
            return None

        return self.llvmgen.create_jump(self.llvmgen.end_block)

    def return_rule(self, tree: Tree):
        nodes = tree.children

        if self.llvmgen.current_block.block.terminator is not None:
            log_fail(f"'return' after return found!")
            return None

        return_val = self.llvmgen.create_return_statement(self.transform_helper(nodes[0]))

        return return_val

    def loop_loop(self, tree: Tree):
        nodes = tree.children
        name = self.llvmgen.current_block.block.name

        (conditional_block, body_block, end_block) = self.llvmgen.create_loop(name,
                                                                              self.llvmgen.current_block)

        # Remove conditional block again (there's no condition)
        self.llvmgen.current_func.basic_blocks.remove(conditional_block.block)
        self.llvmgen.conditional_block = body_block
        del conditional_block

        # Go into body
        self.llvmgen.create_jump(body_block)
        self.llvmgen.enter_block(body_block)

        # Build body
        for node in nodes:
            self.transform_helper(node)

        # Jump back into body
        self.llvmgen.create_jump_if_not_exists(body_block)

        # Go out of loop
        self.llvmgen.enter_block(end_block)

    def for_loop(self, tree: Tree):
        nodes = tree.children
        name = self.llvmgen.current_block.block.name
        name = f"{name}.wrapper"

        wrapper_block = self.llvmgen.create_block(name, parent=self.llvmgen.current_block)
        (conditional_block, body_block, end_block) = self.llvmgen.create_loop(name, wrapper_block)

        # Move end_block out of wrapper block
        end_block.sibling = self.llvmgen.current_block

        # Create variable in wrapper block
        self.llvmgen.create_jump(wrapper_block)
        self.llvmgen.enter_block(wrapper_block)
        self.transform_helper(nodes[0])

        # Enter conditional block
        self.llvmgen.create_jump(conditional_block)
        self.llvmgen.enter_block(conditional_block)

        # Build condition
        condition = self.transform_helper(nodes[1])
        self.llvmgen.create_conditional_jump(condition, body_block, end_block)

        # Go into body
        self.llvmgen.enter_block(body_block)

        # Build body
        i = 3
        while i < len(nodes):
            self.transform_helper(nodes[i])
            i += 1

        # Build incrementor if no terminator yet
        if self.llvmgen.current_block.block.terminator is None:
            self.transform_helper(nodes[2])

        # Jump back into condition
        self.llvmgen.create_jump_if_not_exists(conditional_block)

        # Go out of loop
        self.llvmgen.enter_block(end_block)

    def while_loop(self, tree: Tree):
        nodes = tree.children
        name = self.llvmgen.current_block.block.name

        (conditional_block, body_block, end_block) = self.llvmgen.create_loop(name,
                                                                              self.llvmgen.current_block)

        # Enter conditional block
        self.llvmgen.create_jump(conditional_block)
        self.llvmgen.enter_block(conditional_block)

        # Build condition
        condition = self.transform_helper(nodes[0])
        self.llvmgen.create_conditional_jump(condition, body_block, end_block)

        # Go into body
        self.llvmgen.enter_block(body_block)

        # Build body
        i = 1
        while i < len(nodes):
            self.transform_helper(nodes[i])
            i += 1

        # Jump back into condition
        self.llvmgen.create_jump_if_not_exists(conditional_block)

        # Leave loop
        self.llvmgen.enter_block(end_block)

    def conditional_block(self, tree: Tree):
        nodes = tree.children

        name = self.llvmgen.current_block.block.name
        else_block = None

        if len(nodes) == 2:
            (conditional_block, body_block, end_block) = \
                self.llvmgen.create_conditional_block(name, self.llvmgen.current_block)
        else:
            (conditional_block, body_block, else_block, end_block) = \
                self.llvmgen.create_conditional_block_with_else(name,
                                                                self.llvmgen.current_block)

        # Create condition
        self.llvmgen.create_jump(conditional_block)
        self.llvmgen.enter_block(conditional_block)
        cond = self.transform_helper(nodes[0])
        self.llvmgen.create_conditional_jump(cond, body_block, else_block is not None and else_block or end_block)

        # Create body
        self.llvmgen.enter_block(body_block)
        self.transform_helper(nodes[1])

        # Jump out of body
        self.llvmgen.create_jump_if_not_exists(end_block)

        # Create else if necessary
        if len(nodes) > 2:
            self.llvmgen.enter_block(else_block)
            self.transform_helper(nodes[2])
            self.llvmgen.create_jump_if_not_exists(end_block)

        # Leave conditional block
        self.llvmgen.enter_block(end_block)

    def shorthand_if(self, tree: Tree):
        nodes = tree.children
        name = self.llvmgen.current_block.block.name
        (conditional_block, body_block, else_block, end_block) = \
            self.llvmgen.create_conditional_block_with_else(name, self.llvmgen.current_block)

        # Create condition
        self.llvmgen.create_jump(conditional_block)
        self.llvmgen.enter_block(conditional_block)
        cond = self.transform_helper(nodes[0])
        self.llvmgen.create_conditional_jump(cond, body_block, else_block)

        # Create body
        self.llvmgen.enter_block(body_block)
        true_value = self.transform_helper(nodes[1])

        # Jump out of body
        self.llvmgen.create_jump_if_not_exists(end_block)

        # Create else
        self.llvmgen.enter_block(else_block)
        false_value = self.transform_helper(nodes[2])
        self.llvmgen.create_jump_if_not_exists(end_block)

        # Leave conditional block
        self.llvmgen.enter_block(end_block)

        # PHI the values
        phi = self.llvmgen.builder.phi(true_value.type)
        phi.add_incoming(true_value, body_block.block)
        phi.add_incoming(false_value, else_block.block)

        return phi

    def switch_block(self, tree: Tree):
        nodes = tree.children
        parent = self.llvmgen.current_block
        variable = self.llvmgen.gen_load_if_necessary(self.transform_helper(nodes[0]))
        end_block = self.llvmgen.create_block(f"{self.llvmgen.current_block.block.name}.end_switch",
                                              sibling=self.llvmgen.current_block)
        self.llvmgen.end_block = end_block
        switch = self.llvmgen.builder.switch(variable, None)
        collected_empties = list()

        i = 1
        while i < len(nodes):
            var, block = self.transform_helper(nodes[i])

            # Check if just case, collect for future use
            if block is None:
                collected_empties.append(var)
            else:
                self.llvmgen.enter_block_end(block)

                # Check if default case and set as default
                if var is None:
                    if switch.default is not None:
                        log_fail(f"{ParserState.module().name} Duplicate default case in switch statement")
                    else:
                        switch.default = block.block
                else:
                    switch.add_case(var, block.block)

                # Add all collected cases
                for collected_empty in collected_empties:
                    switch.add_case(collected_empty, block.block)

                collected_empties.clear()

            self.llvmgen.enter_block_end(parent)

            i += 1

        if switch.default is None:
            switch.default = end_block.block

        if len(collected_empties) > 0:
            log_fail("Empty cases in switch statement!")

        self.llvmgen.end_block = None
        self.llvmgen.enter_block(end_block)

    def switch_case(self, tree: Tree):
        nodes = tree.children
        var = self.llvmgen.gen_load_if_necessary(self.transform_helper(nodes[0]))

        if len(nodes) == 1:
            return var, None
        block = self.llvmgen.create_block(f"{self.llvmgen.current_block.block.name}.switch.case",
                                          self.llvmgen.current_block)
        self.llvmgen.enter_block(block)

        i = 1
        while i < len(nodes):
            self.transform_helper(nodes[i])
            i += 1

        self.llvmgen.finish_current_block()

        return var, block

    def default_case(self, tree: Tree):
        nodes = tree.children
        block = self.llvmgen.create_block(f"{self.llvmgen.current_block.block.name}.switch.default",
                                          self.llvmgen.current_block)
        self.llvmgen.enter_block(block)
        for node in nodes:
            self.transform_helper(node)

        self.llvmgen.finish_current_block()

        return None, block

    def struct_decl(self, tree: Tree):
        nodes = tree.children
        node = nodes[0]
        full_name = node.metadata['struct_name']
        function_decls = node.metadata['functions']

        struct = ParserState.search_structs(full_name)

        if struct is None:
            raise KeyError(f"Expected a struct {full_name} but couldn't find it!")

        self.llvmgen.current_struct = struct

        # Create functions
        for function_decl in function_decls:
            self.visit(function_decl)

        self.llvmgen.finish_struct()

    def constructor_call(self, tree: Tree):
        nodes = tree.children
        name = nodes[0]
        struct = ParserState.find_struct(name)

        if struct is None:
            log_fail(f"Instantiation of unknown type {name}")

        arguments = list()

        instantiated = self.llvmgen.builder.alloca(struct)
        arguments.append(instantiated)

        arguments.extend(self.transform_helper(nodes[1]))

        constructor_name = mangle_function_name("constructor", [arg.type for arg in arguments], name)

        try:
            call_instr = self.llvmgen.gen_function_call([constructor_name], arguments)

            if call_instr is None:
                log_fail(f"Failed to generate call to function {constructor_name}")
                return NULL

            return instantiated
        except IndexError:
            log_fail("Missing argument in function call")

        return NULL

    def nested_function_call(self, tree: Tree):
        nodes = tree.children
        i = 0
        full_name = ""
        arguments = list()
        while i < len(nodes):
            if not isinstance(nodes[i], Token):
                break

            full_name += f".{nodes[i].value}"
            i += 1

        implicit_parameter_name = '.'.join(full_name.split('.')[0:-1])
        function_name = full_name.split('.')[-1]
        implicit_parameter = self.llvmgen.get_var(implicit_parameter_name)

        if implicit_parameter is None:
            log_fail(f"Could not find implicit parameter {implicit_parameter_name} in function call {full_name}")
            return NULL

        arguments.append(implicit_parameter)
        arguments.extend(self.transform_helper(nodes[i]))

        arg_types = [arg.type for arg in arguments]
        mangled_names = list()

        # Generate mangled names for implicit parameter and derived
        if implicit_parameter is not None:
            if isinstance(implicit_parameter.type, PointerType):
                ty = implicit_parameter.type.pointee
            else:
                ty = implicit_parameter.type

            ty = map_llvm_to_type(ty)

            # Check if it's a builtin type
            if isinstance(ty, str) and ty in ParserState.builtin_types:
                mangled_names.append(mangle_function_name(function_name, [arg.type for arg in arguments], ty))
            else:
                mangled_names.append(mangle_function_name(function_name, [arg.type for arg in arguments], ty.name))

                struct = ParserState.find_struct(ty.name)

                # Also mangle base structs to see if it's a derived function
                for base_struct in struct.definition.base_structs:
                    arg_tys = arg_types
                    arg_tys.pop(0)
                    arg_tys.insert(0, base_struct)
                    mangled_names.append(
                        mangle_function_name(function_name, arg_types, base_struct))
        else:
            mangled_names.append(mangle_function_name(function_name, arg_types))

        try:
            call_instr = self.llvmgen.gen_function_call([*mangled_names, function_name], arguments)

            if call_instr is None:
                log_fail(f"Failed to generate call to function {function_name}")
                return NULL

            return call_instr
        except IndexError:
            log_fail(f"Missing argument in function call to function {function_name}")

        return NULL

    def function_call(self, tree: Tree):
        nodes = tree.children
        full_function_name: str
        function_name: str

        # Function call specialisation
        if isinstance(nodes[0], Tree):
            return self.visit(nodes[0])

        function_name = nodes[0].value
        arguments = self.transform_helper(nodes[1])

        arg_types = [arg.type for arg in arguments]

        try:
            call_instr = self.llvmgen.gen_function_call([mangle_function_name(function_name, arg_types), function_name],
                                                        arguments)

            if call_instr is None:
                log_fail(f"Failed to generate call to function {function_name}")
                return NULL

            return call_instr
        except IndexError:
            log_fail("Missing argument in function call")

        return NULL

    def function_args(self, tree: Tree):
        nodes = tree.children
        arguments = list()
        for node in nodes:
            arguments.append(self.transform_helper(node))

        return arguments

    def function_decl(self, tree: Tree):
        nodes = tree.children

        node: MetadataToken = nodes[0]
        body_start = node.metadata['body_start']
        function_name = node.metadata['full_name']
        rial_arg_types = node.metadata['rial_arg_types']
        func = next((func for func in ParserState.module().functions if func.name == function_name), None)

        if func is None:
            raise KeyError("Expected a function but didn't find it!")

        self.llvmgen.create_function_body(func, rial_arg_types)

        i = body_start
        while i < len(nodes):
            self.transform_helper(nodes[i])
            i += 1

        self.llvmgen.finish_current_block()
        self.llvmgen.finish_current_func()

        return None
