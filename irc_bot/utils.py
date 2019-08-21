import asyncio


async def execute_command(command):
    process = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await process.communicate()

    return stdout.decode().split("\n")


async def say_execute_command(bot, command, channel=None):
    result = await execute_command(command)

    for line in result:
        if channel:
            await bot.say(line, channel)
        else:
            await bot.say(line)
