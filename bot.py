import ast
import contextvars
import pathlib
import sys
from io import StringIO

from discord.ext import commands

bot = commands.Bot('py ', description='Created by A\u200bva#4982', help_command=commands.MinimalHelpCommand())


class StdoutProxy:
    def __init__(self):
        self.var = contextvars.ContextVar('stdout')

    def write(self, *args, **kwargs):
        out = self.var.get(sys.stdout)
        out.write(*args, **kwargs)

    def __getattr__(self, item):
        return getattr(self.var.get(sys.stdout), item)

    def set(self, val):
        self.var.set(val)

    def get(self):
        return self.var.get().getvalue()


_stdout = StdoutProxy()
sys.stdout = _stdout


def cleaned_code(arg):
    arg = arg.strip('`')
    if arg.startswith('py'):
        arg = arg[2:]

    return arg.strip()


class Eval(commands.Cog):
    base_code = 'async def func_():\n\tyield ...'
    last_val = ...

    def transform(self, statement: ast.stmt):
        """
        Adds an implicit yield to the last proper expression of the code
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

    def create_namespaces(self, ctx):
        globals_ = {
            '_': self.last_val,
            'ctx': ctx,
            'bot': ctx.bot,
            'guild': ctx.guild,
            'server': ctx.guild,
            'author': ctx.author,
            'channel': ctx.channel,
        }
        globals_.update(sys.modules)
        globals_.update(globals())

        locals_ = {}

        return globals_, locals_

    @commands.is_owner()
    @commands.command(name='eval')
    async def _eval(self, ctx, *, code: cleaned_code):
        base_tree = ast.parse(self.base_code)
        async_func = base_tree.body[0]

        code = ast.parse(code)
        async_func.body.extend(code.body)

        self.transform(async_func)
        ast.fix_missing_locations(base_tree)

        code = compile(base_tree, '<eval>', 'exec')

        globals_, locals_ = self.create_namespaces(ctx)

        exec(code, globals_, locals_)

        func = locals_['func_']

        _stdout.set(StringIO())

        add_reaction = False
        results = []
        async for result in func():
            if result is ...:
                continue

            if result is None:
                add_reaction = True
                continue

            self.last_val = result
            results.append(repr(result))

        if add_reaction:
            await ctx.message.add_reaction(':check:572005045407842314')

        out = ''
        if results:
            out += '\nResult{}:\n'.format('s' * (len(results) > 1))
            out += '\n'.join(results)
            out += '\n'

        stdout = _stdout.get()
        if stdout:
            out += '\nstdout:\n'
            out += stdout
            out += '\n'

        if out:
            await ctx.send(out.strip())


bot.add_cog(Eval())


@bot.command()
async def info(ctx):
    await ctx.send(ctx.bot.description)


if __name__ == '__main__':
    bot.run(pathlib.Path('token.txt').read_text())
