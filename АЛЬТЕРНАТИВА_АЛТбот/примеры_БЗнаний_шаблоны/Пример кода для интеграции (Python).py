import json
from datetime import datetime
from typing import Dict, Optional, Any

class TemplateEngine:
    def __init__(self, templates_path: str, triggers_path: str):
        with open(templates_path, 'r', encoding='utf-8') as f:
            self.templates = json.load(f)
        with open(triggers_path, 'r', encoding='utf-8') as f:
            self.triggers = json.load(f)
        
        self.variables = self.templates.get('variables', {})
        self.usage_log = []
    
    def find_trigger(self, user_text: str) -> Optional[Dict]:
        """Находит триггер по ключевым словам в запросе пользователя"""
        user_text_lower = user_text.lower()
        
        for trigger in self.triggers.get('triggers', []):
            for keyword in trigger.get('keywords', []):
                if keyword.lower() in user_text_lower:
                    return trigger
        
        return None
    
    def load_template(self, template_key: str, template_type: str) -> Optional[str]:
        """Загружает шаблон по ключу и типу"""
        try:
            template_data = self.templates[template_type][template_key]
            return template_data.get('text', '')
        except KeyError:
            return None
    
    def substitute_variables(self, text: str, custom_vars: Optional[Dict] = None) -> str:
        """Подставляет переменные в текст шаблона"""
        variables = {**self.variables, **(custom_vars or {})}
        
        for key, value in variables.items():
            text = text.replace(f'{{{key}}}', str(value))
        
        return text
    
    def get_response(self, user_text: str, user_context: Optional[Dict] = None) -> Dict[str, Any]:
        """Основной метод: получает запрос → возвращает готовый ответ"""
        trigger = self.find_trigger(user_text)
        
        if trigger:
            template_text = self.load_template(
                trigger['template_key'],
                trigger['template_type']
            )
            
            if template_text:
                # Подстановка переменных
                final_text = self.substitute_variables(template_text, user_context)
                
                # Логирование
                self.log_usage(trigger, user_context)
                
                # Проверка на keyboard (для интерактивных шаблонов)
                keyboard = None
                template_data = self.templates[trigger['template_type']][trigger['template_key']]
                if 'keyboard' in template_data:
                    keyboard = self._build_keyboard(template_data['keyboard'])
                
                return {
                    'success': True,
                    'text': final_text,
                    'trigger_id': trigger['id'],
                    'template_key': trigger['template_key'],
                    'keyboard': keyboard
                }
        
        # Fallback
        fallback = self.triggers.get('fallback', {})
        fallback_text = self.load_template(fallback.get('template_key'), fallback.get('template_type'))
        
        return {
            'success': False,
            'text': self.substitute_variables(fallback_text or "Извините, я не понял вопрос. Напишите «помощь»."),
            'trigger_id': 'fallback',
            'template_key': fallback.get('template_key'),
            'keyboard': None
        }
    
    def log_usage(self, trigger: Dict, context: Optional[Dict] = None):
        """Логирует использование шаблона для аналитики"""
        log_entry = {
            'timestamp': datetime.now().isoformat(),
            'trigger_id': trigger.get('id'),
            'template_key': trigger.get('template_key'),
            'template_type': trigger.get('template_type'),
            'user_id': context.get('user_id') if context else None,
            'platform': context.get('platform') if context else 'vk_messenger'
        }
        self.usage_log.append(log_entry)
    
    def _build_keyboard(self, keyboard_config: Dict) -> Dict:
        """Строит keyboard для VK API из конфигурации"""
        buttons = []
        for btn in keyboard_config.get('buttons', []):
            button = {
                'action': {
                    'type': btn.get('action', 'text'),
                },
                'label': btn.get('label', '')
            }
            if btn.get('link'):
                button['action']['link'] = btn['link']
            if btn.get('payload'):
                button['action']['payload'] = btn['payload']
            buttons.append(button)
        
        return {
            'one_time': False,
            'inline': True,
            'buttons': [buttons]
        }
    
    def get_usage_report(self) -> Dict:
        """Возвращает отчёт по использованию шаблонов"""
        report = {}
        for log in self.usage_log:
            key = f"{log['template_type']}::{log['template_key']}"
            report[key] = report.get(key, 0) + 1
        return report