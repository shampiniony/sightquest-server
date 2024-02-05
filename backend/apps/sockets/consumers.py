import os
import secrets

import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'your_project_name.settings')
django.setup()

import json

from apps.api.models import Game, GameUser, GameQuestTask, GameSettings, GamePhoto
from asgiref.sync import async_to_sync
from channels.generic.websocket import WebsocketConsumer
from django.contrib.auth import get_user_model
from .game.gamestatemanager import GameStateManager, GameState
from apps.api.models import PlayerTaskCompletion
from datetime import timedelta, datetime
from django.utils import timezone

User = get_user_model()


class GameConsumer(WebsocketConsumer):
    game_code: str
    game_group_name: str
    game_state: GameState
    user: User = None
    game_user: GameUser = None

    @staticmethod
    def login_required(func):
        def wrapper(self, *args, **kwargs):
            if self.user is None:
                self.send_status_message("You didnt complete authorization")
                self.close()
                return
            func(self, *args, **kwargs)

        return wrapper

    def connect(self):
        self.game_code = str(self.scope["url_route"]["kwargs"]["game_id"])
        self.game_state = GameStateManager.get_game_state(self.game_code)

        self.game_group_name = "game_%s" % self.game_code

        async_to_sync(self.channel_layer.group_add)(
            self.game_group_name, self.channel_name
        )
        self.accept()

        self.send_status_message("connection succeed")

    def receive(self, text_data):
        text_data_json = json.loads(text_data)
        event_type = text_data_json["event"]
        if event_type == "authorization":
            self.on_receive_authorization(text_data_json)
        elif event_type == "get_game_state":
            self.on_receive_send_game_state(text_data_json)
        elif event_type == "location_update":
            self.on_receive_location_update(text_data_json)
        elif event_type == "task_completed":
            self.on_receive_task_completed(text_data_json)
        elif event_type == "player_caught":
            self.on_receive_player_caught(text_data_json)
        elif event_type == "settings_update":
            self.on_receive_settings_update(text_data_json)
        elif event_type == "start_game":
            self.on_receive_start_game(text_data_json)
        else:
            self.group_broadcast(text_data_json)

    def send_game_state(self):
        self.send(text_data=json.dumps(
            {
                "event": "gamestate_update",
                "state": self.game_state.process_to_json()
            }
        ))

    @login_required
    def group_broadcast(self, data):
        async_to_sync(self.channel_layer.group_send)(
            self.game_group_name, {
                "type": "broadcast",
                "sender_id": self.user.id,
                "data": data
            }
        )

    def send_status_message(self, message):
        self.send(json.dumps({
            "event": "status",
            "message": message
        }))

    def broadcast(self, data):
        self.send(text_data=json.dumps(data["data"]))

    @login_required
    def on_receive_location_update(self, data_json):
        self.game_state.set_player_coordinates(self.user.id, data_json["coordinates"])
        self.group_broadcast(data_json)

    def on_receive_authorization(self, data_json):
        self.user = User.objects.filter(id=int(data_json["token"])).first()
        if self.user is not None:
            self.game_state.add_player(self.user)
            self.game_user = GameUser.objects.filter(game=self.game_state.game, user=self.user).first()
            self.send_status_message(f"authorization succeed as {self.user}")
        else:
            self.send_status_message("authorization failed")
            self.close()

    @login_required
    def on_receive_send_game_state(self, data_json):
        self.send_game_state()

    def disconnect(self, close_code):
        # Leave room group
        async_to_sync(self.channel_layer.group_discard)(
            self.game_group_name,
            self.channel_name
        )

    @login_required
    def on_receive_task_completed(self, data_json):
        try:
            task_id = int(data_json["task_id"])
            photo_id = data_json["photo_id"]
            game_photo: GamePhoto = GamePhoto.objects.filter(id=photo_id).first()

            game_task = GameQuestTask.objects.filter(settings__game=self.game_state.game, quest_task_id=task_id).first()
            PlayerTaskCompletion(
                photo=game_photo,
                game_task=game_task,
                player=self.game_user
            ).save()
            self.game_state.update_from_db()
            self.group_broadcast(data_json)
        except Exception:
            self.send_status_message("Your data bad somehow")

    @login_required
    def on_receive_player_caught(self, data_json):
        try:
            secret = data_json["secret"]
            game_user: GameUser = GameUser.objects.filter(game=self.game_state.game, secret=secret).first()
            if game_user is None:
                self.send_status_message("secret does not exists")
                return
            if game_user.role != "RUNNER":
                self.send_status_message("player is not a runner")
                return

            players = [player for player in self.game_state.game.players.order_by("order_key")]

            player_index = players.index(game_user)
            next_runner_index = (player_index + 1) % len(players)
            game_user.role = "CATCHER"
            game_user.regenerate_secret()
            game_user.save()
            players[next_runner_index].role = "RUNNER"
            players[next_runner_index].save()

            self.game_state.update_from_db()
            self.group_broadcast(data_json)
        except Exception:
            self.send_status_message("Your data bad somehow")

    @login_required
    def on_receive_settings_update(self, data_json):
        settings = data_json["settings"]
        quest_points_data = settings["quest_points"]
        duration = settings["duration"]
        game_settings: GameSettings = self.game_state.game.settings

        t = datetime.strptime(duration, "%H:%M:%S")
        game_settings.duration = timedelta(hours=t.hour, minutes=t.minute, seconds=t.second)

        game_settings.tasks.clear()
        for quest_point_data in quest_points_data:
            for quest_task_data in quest_point_data["tasks"]:
                quest_task_id = quest_task_data["id"]
                game_quest_task = GameQuestTask(settings=game_settings, quest_task_id=quest_task_id)
                game_quest_task.save()

        game_settings.save()
        self.game_state.update_from_db()
        self.group_broadcast(data_json)

    @login_required
    def on_receive_start_game(self, data_json):
        self.game_state.game.started_at = timezone.now()
        players = [player for player in self.game_state.game.players.all()]
        for player in players:
            player.role = "CATCHER"
            player.save()
        runner = secrets.choice(players)
        runner.role = "RUNNER"
        runner.save()

        self.game_state.game.state = "PLAYING"
        self.game_state.update_from_db()
        self.group_broadcast(data_json)
