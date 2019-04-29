import ast
import contextlib
import contextvars
import pathlib
import sys
import textwrap
import traceback
from io import StringIO

import discord
from discord.ext import commands

bot = commands.Bot('py ', description='Created by A\u200bva#4982', help_command=commands.MinimalHelpCommand())


@contextlib.asynccontextmanager
async def aclosing(gen):
    """
    Async version of `contextlib.closing`
    """
    try:
        yield gen
    finally:
        await gen.aclose()


class StdoutProxy:
    def __init__(self):
        self.var = contextvars.ContextVar('stdout')
        self.original = sys.stdout

    def write(self, *args, **kwargs):
        out = self.var.get(self.original)
        out.write(*args, **kwargs)

    def __getattr__(self, item):
        return getattr(self.var.get(self.original), item)

    def set(self, val):
        self.var.set(val)

    def get(self):
        return self.var.get().getvalue()


_stdout = StdoutProxy()
sys.stdout = _stdout


def cleaned_code(arg):
    """
    Strip discord code blocks
    """
    arg = arg.strip('`')
    if arg.startswith('py'):
        arg = arg[2:]

    return arg.strip()


class ReturnException(Exception):
    """
    Used to mock a return
    """
    pass


class Eval(commands.Cog):
    # I use ... as a sentinel value in the `eval` command. Having a yield here unconditionally makes the resulting
    # function an async generator which makes it nicer to handle than using `if inspect.isasyncgenfunction`.
    base_code = (
        'async def func_():\n'
        '    yield ...'
    )

    # mimics the above behaviour, ... is a sentinel value
    last_val = ...

    def transform(self, statement: ast.stmt):
        """
        Adds an implicit yield to the last expression in the function body if it's yield-able
        """

        body = getattr(statement, 'body', statement)

        # we only want to look at the last statement in the code
        statement = body.pop()

        if isinstance(statement, ast.Expr):
            # if it's an expression turn the expression into a yield expression instead
            statement.value = ast.Yield(value=statement.value)
        elif isinstance(statement, ast.If):
            # we have to transform the body of the if statement, so let's just use recursion
            self.transform(statement)

            # if the statement has elif or else branches we need to visit those too
            if statement.orelse:
                self.transform(statement.orelse)
        elif isinstance(statement, ast.Try):
            # transform try block
            self.transform(statement)

            # transform each except block
            for handler in statement.handlers:
                self.transform(handler)

            # transform finally if it exists
            if statement.finalbody:
                self.transform(statement.finalbody)

            # transform else if it exists
            if statement.orelse:
                self.transform(statement.orelse)

        # add the (potentially) transformed statement back to the function body
        body.append(statement)

    def replace_returns(self, tree):
        """
        Async generators cannot have `return`s inside them,
        so this function replaces each `return` in the function body with a `raise` that raises a custom exception.

        This effectively has the same effect without causing a SyntaxError
        """

        for index, val in enumerate(tree):  # need to keep track of index to be able to replace returns
            # if the node in the tree has a body (e.g. if and try block) we want to recursively adjust that too.
            if hasattr(val, 'body'):
                self.replace_returns(val.body)
            # if we see a return, we want to replace it
            elif isinstance(val, ast.Return):
                tree[index] = ast.Raise(
                    exc=ast.Call(
                        # `return abc` -> `raise ReturnException(abc)`
                        args=[val.value],
                        # basically just loads the name `ReturnException`
                        func=ast.Name(
                            id='ReturnException', ctx=ast.Load(), lineno=val.lineno, col_offset=val.col_offset
                        ),
                        # these need to be provided though the values don't really matter
                        keywords=[],
                        lineno=val.lineno,
                        col_offset=val.col_offset,
                    ),
                    # these also need to be provided though the values don't really matter
                    cause=None,
                    lineno=val.lineno,
                    col_offset=val.col_offset
                )

    def create_namespaces(self, ctx):
        """
        Create the `globals` and `locals` namespaces for the `exec` function
        """
        # utility stuff
        globals_ = {
            '_': self.last_val,
            'ctx': ctx,
            'bot': ctx.bot,
            'guild': ctx.guild,
            'server': ctx.guild,
            'author': ctx.author,
            'channel': ctx.channel,
        }
        # imports and other global variables
        globals_.update(globals())

        # we fetch the compiled function from this later on, but don't need to put anything in it
        locals_ = {}

        return globals_, locals_

    @commands.is_owner()
    @commands.command(name='eval')
    async def _eval(self, ctx, *, code: cleaned_code):
        """
        Evaluate Python code

        TODO write a better docstring here
        """

        # create the AST tree to transform
        base_tree = ast.parse(self.base_code)
        # do .body[0] to get the actual `async def`, since everything is wrapped in an `ast.Module`
        async_func = base_tree.body[0]

        try:
            # create the AST tree for the code we want to execute
            code = ast.parse(code)
        except SyntaxError:
            # oops the user did something silly, we should let them know.
            await ctx.message.add_reaction(':warning:572102707331203075')
            embed = discord.Embed(colour=discord.Colour.red())
            embed.add_field(
                name='**Traceback**',
                value=f'```{traceback.format_exc(limit=2)}```'
            )
            return await ctx.send(embed=embed)

        # put all the executable code into the body of the base function after the `yield ...` line
        async_func.body.extend(code.body)

        # replace all `return`s with a custom raise, see docstring of function
        self.replace_returns(async_func.body)
        self.transform(async_func)
        # we need to call this or it's all fucked, don't ask me why
        ast.fix_missing_locations(base_tree)

        # turn the AST into a code object
        code = compile(base_tree, '<eval>', 'exec')

        globals_, locals_ = self.create_namespaces(ctx)

        exec(code, globals_, locals_)

        # get the
        func = locals_['func_']

        # we capture the `print`s in the compiled code into a `StringIO` object so we can observe the `stdout` output
        _stdout.set(StringIO())

        # if a `None` was yielded/returned we want to acknowledge it without showing it in the result message
        add_reaction = False

        # whether an exception occurred
        exception = None

        results = []
        agen = func()
        try:
            # we need to close the async generator in case it raises or it's all bad for some reason I don't understand
            # see https://www.python.org/dev/peps/pep-0533/
            async with aclosing(agen):
                # explicit `yield`s in the body or the last return value
                async for result in agen:
                    # this is the sentinel we want to ignore
                    if result is ...:
                        continue

                    # we don't wanna show None in the output but still wanna know it's there
                    if result is None:
                        add_reaction = True
                        continue

                    # otherwise show the result
                    results.append(repr(result))
                    # update the _ magic var like in the REPL
                    self.last_val = result
        except ReturnException as e:
            # see the thing about no `return`s in async generators
            results.extend(repr(arg) for arg in e.args)
        except Exception:
            # if we got a legitimate exception we wanna show that
            exception = traceback.format_exc(limit=2)

        if add_reaction:
            await ctx.message.add_reaction(':check:572005045407842314')

        embed = discord.Embed()

        if results:
            plural = len(results) > 1
            embed.add_field(
                name=f'**Result{"s" * plural}:**',
                value='```py\n' + textwrap.indent('\n'.join(results), ' \N{BULLET OPERATOR} ') + '```'
            )

        stdout = _stdout.get()
        if stdout:
            embed.add_field(
                name='**stdout**',
                value=f'```{stdout}```'
            )

        if exception:
            await ctx.message.add_reaction(':warning:572102707331203075')
            embed.colour = discord.Colour.red()
            embed.add_field(
                name='**Traceback**',
                value=f'```{exception}```'
            )
        else:
            embed.colour = discord.Colour.green()

        # if we didn't have anything then don't just send an empty embed
        if embed.fields:
            await ctx.send(embed=embed)


bot.add_cog(Eval())


@bot.command()
async def info(ctx):
    await ctx.send(ctx.bot.description)


if __name__ == '__main__':
    bot.run(pathlib.Path('token.txt').read_text())
