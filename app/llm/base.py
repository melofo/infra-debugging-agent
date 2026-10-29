"""LLM 抽象层：定义"和大模型对话"这件事的统一接口。

为什么要抽象？业务代码（Agent、/chat）只依赖这里的接口，不关心背后是 Gemini 还是 OpenAI。
换模型只需新增一个实现类，不用改业务代码；测试时还能换成假的实现，不花钱不联网。
这就是面向接口编程，和 Java 里的做法一致。
"""

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


class LLMError(Exception):
    """LLM 调用失败（网络、限流、返回异常等）。

    把各家 SDK 五花八门的异常统一包装成这一种，上层只需要 catch 它。
    """


# @dataclass 自动生成构造器、equals、toString，相当于 Java 的 record。
# frozen=True 表示创建后不可修改（不可变对象，像 record 一样）。


# 工具声明：发给模型看的"说明书"，告诉它有哪些工具、每个工具干什么、要什么参数。
# 注意它只是【描述】，不包含可执行的代码。模型读了说明书后，只能"提议"调用某个工具；
# 真正执行的是我们后端（见 app/agent/tools.py）。
# parameters 是 JSON Schema（一种描述 JSON 结构的标准格式），各家 LLM 厂商都用它描述参数。
@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str  # 写给模型看的：什么时候该用这个工具。写得好坏直接影响模型选工具的准确率
    parameters: dict


# 模型说"我想调用某个工具"。注意是请求，不是已经执行了。
@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict


# 我们执行完工具后，把结果装进这个对象交还给模型。
@dataclass(frozen=True)
class ToolResult:
    name: str
    result: Any  # 可 JSON 序列化的数据；失败时是错误说明文字
    is_error: bool = False


# 对话历史里的一条消息。模型本身没有记忆，所以每一轮都要把完整历史重新发给它。
# role 有三种：
#   user  - 用户（或我们的系统）说的话
#   model - 模型说的话，可能带 tool_calls（它想调用工具）
#   tool  - 我们回传的工具执行结果
@dataclass(frozen=True)
class Message:
    role: Literal["user", "model", "tool"]
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_results: tuple[ToolResult, ...] = ()
    # raw：厂商专属的原始数据，我们不解析，只是原样存着，下一轮再原样发回去。
    # 为什么需要？有些模型（如较新的 Gemini）会在函数调用里附带内部标记（thought signature），
    # 要求下一轮必须原样带回，否则请求会被拒绝。把原始输出整个保存下来是最稳妥的做法。
    raw: Any = field(default=None, compare=False)


@dataclass(frozen=True)
class LLMResponse:
    text: str
    input_tokens: int  # 提示词消耗的 token 数；token 是 LLM 计费和计算的基本单位（约等于半个到一个词）
    output_tokens: int  # 模型回答消耗的 token 数
    latency_ms: int  # 这次调用耗时，后面做延迟和成本统计要用
    # 模型这一轮想调用的工具。空元组表示它直接给出了文字回答。
    tool_calls: tuple[ToolCall, ...] = ()
    raw: Any = field(default=None, compare=False)


# Protocol 是 Python 的"结构化接口"：不需要写 implements。
# 只要一个类有同名同签名的方法，就算实现了它（鸭子类型 duck typing）。
# 对比 Java：Java 要显式 `implements LLMClient`，Python 只看"长得像不像"。
class LLMClient(Protocol):
    # async def：异步方法。等待网络响应时，服务器可以去处理别的请求，不会卡住线程。
    # 调用时要写 await；类似 Java 的 CompletableFuture，但语法直观得多。

    # 简单问答：一问一答，不带工具（/chat 使用）。
    async def generate(self, prompt: str, system: str | None = None) -> LLMResponse: ...

    # 带工具的一轮对话：给完整历史 + 工具说明书，模型回复"文字"或"想调用的工具"。
    # 注意：每次调用只做【一次】模型请求。循环由调用方（Agent）负责。
    async def generate_with_tools(
        self, messages: list[Message], tools: list[ToolSpec], system: str | None = None
    ) -> LLMResponse: ...
