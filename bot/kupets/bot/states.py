"""FSM-состояния бота."""

from aiogram.fsm.state import State, StatesGroup


class SearchStates(StatesGroup):
    idle = State()
    has_results = State()
    awaiting_error_report = State()
