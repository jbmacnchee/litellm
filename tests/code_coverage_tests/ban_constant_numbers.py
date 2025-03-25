import sys
import ast
import os

# Extremely restrictive set of allowed numbers
ALLOWED_NUMBERS = {0, 1, -1, 2, 10, 100}


class HardcodedNumberFinder(ast.NodeVisitor):
    def __init__(self):
        self.hardcoded_numbers = []

    def visit_Constant(self, node):
        # For Python 3.8+
        if isinstance(node.value, (int, float)) and node.value not in ALLOWED_NUMBERS:
            self.hardcoded_numbers.append((node.lineno, node.value))
        self.generic_visit(node)

    def visit_Num(self, node):
        # For older Python versions
        if node.n not in ALLOWED_NUMBERS:
            self.hardcoded_numbers.append((node.lineno, node.n))
        self.generic_visit(node)


def check_file(filename):
    try:
        with open(filename, "r") as f:
            content = f.read()

        tree = ast.parse(content)
        finder = HardcodedNumberFinder()
        finder.visit(tree)

        if finder.hardcoded_numbers:
            print(f"ERROR in {filename}: Hardcoded numbers detected:")
            for line, value in finder.hardcoded_numbers:
                print(f"  Line {line}: {value}")
            return 1
        return 0
    except SyntaxError:
        print(f"Syntax error in {filename}")
        return 0


def main():
    exit_code = 0
    folder = "../../litellm"
    for root, dirs, files in os.walk(folder):
        for filename in files:
            if filename.endswith(".py"):
                full_path = os.path.join(root, filename)
                exit_code |= check_file(full_path)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
