import asyncio


async def check_output_lines(command):
    process = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await process.communicate()

    return stdout.decode().split("\n")


async def execute_command(bot, command, channel=None):
    result = await check_output_lines(command)

    for line in result:
        if channel:
            await bot.say(line, channel)
        else:
            await bot.say(line)
