# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
import warnings
warnings.filterwarnings('ignore', category=DeprecationWarning)

import os
os.environ['MKL_SERVICE_FORCE_INTEL'] = '1'
os.environ['MUJOCO_GL'] = 'egl'
import quaternion
from pathlib import Path

import hydra
import numpy as np
import torch
from dm_env import specs

import wrappers.dmc as dmc
import utils
from logger import Logger
from replay_buffer import ReplayBufferStorage, make_replay_loader
from video import TrainVideoRecorder, VideoRecorder
import wandb

torch.backends.cudnn.benchmark = True


def make_agent(obs_spec, action_spec, cfg):
    cfg.obs_shape = obs_spec.shape
    cfg.action_shape = action_spec.shape
    return hydra.utils.instantiate(cfg)


class Workspace:
    def __init__(self, cfg):
        self.work_dir = Path.cwd()
        print(f'workspace: {self.work_dir}')

        self.cfg = cfg
        utils.set_seed_everywhere(cfg.seed)
        self.device = torch.device(cfg.device)
        self.setup(self.cfg.env)

        self.agent = make_agent(self.train_env.observation_spec(),
                                self.train_env.action_spec(),
                                self.cfg.agent)
        self.timer = utils.Timer()
        self._global_step = 0
        self._global_episode = 0

    def setup(self, env):
        if self.cfg.use_wandb:
            exp_name = '_'.join([
                self.cfg.task_name,
                str(self.cfg.seed)
            ])
            wandb.init(project=self.cfg.wandb_proj_name, group=f'{self.cfg.agent._target_}', name=exp_name)
        # create logger
        self.logger = Logger(self.work_dir, use_tb=self.cfg.use_tb, use_wandb=self.cfg.use_wandb)
        assert env in ['dmc', 'robosuite', 'habitat']
        # create envs
        if env == 'dmc':
            self.train_env = dmc.make(self.cfg.task_name, self.cfg.frame_stack,
                                    self.cfg.action_repeat, self.cfg.seed)
            self.eval_env = dmc.make(self.cfg.task_name, self.cfg.frame_stack,
                                    self.cfg.action_repeat, self.cfg.seed)
        elif env == 'robosuite':
            from wrappers.robo_wrapper import robo_make
            self.train_env = robo_make(name=self.cfg.task_name, action_repeat=self.cfg.action_repeat, 
                                       frame_stack=self.cfg.frame_stack, seed=self.cfg.seed)
            self.eval_env = robo_make(name=self.cfg.task_name, action_repeat=self.cfg.action_repeat, 
                                      frame_stack=self.cfg.frame_stack, seed=self.cfg.seed)
        elif env == 'habitat':
            # from wrappers.carla_wrapper import carla_make
            # self.train_env = carla_make(action_repeat=self.cfg.action_repeat)
            # self.eval_env = self.train_env
            hydra.core.global_hydra.GlobalHydra.instance().clear()
            from wrappers.habi_wrapper import make_habitat_env
            self.train_env = make_habitat_env(name='HabitatImageNav-v0', mode='train', seed=self.cfg.seed, action_repeat=self.cfg.action_repeat)
            self.eval_env = make_habitat_env(name='HabitatImageNav-v0', mode='val', seed=self.cfg.seed, action_repeat=self.cfg.action_repeat)
        else:
            raise ValueError(f"env {env} not supported.")
                
        # create replay buffer
        data_specs = (self.train_env.observation_spec(),
                      self.train_env.action_spec(),
                      specs.Array((1,), np.float32, 'reward'),
                      specs.Array((1,), np.float32, 'discount'))

        self.replay_storage = ReplayBufferStorage(data_specs,
                                                  self.work_dir / 'buffer')

        self.replay_loader = make_replay_loader(
            self.work_dir / 'buffer', self.cfg.replay_buffer_size,
            self.cfg.batch_size, self.cfg.replay_buffer_num_workers,
            self.cfg.save_snapshot, self.cfg.nstep, self.cfg.discount)
        self._replay_iter = None

        self.video_recorder = VideoRecorder(
            self.work_dir if self.cfg.save_video else None)
        self.train_video_recorder = TrainVideoRecorder(
            self.work_dir if self.cfg.save_train_video else None)


    @property
    def global_step(self):
        return self._global_step

    @property
    def global_episode(self):
        return self._global_episode

    @property
    def global_frame(self):
        return self.global_step * self.cfg.action_repeat

    @property
    def replay_iter(self):
        if self._replay_iter is None:
            self._replay_iter = iter(self.replay_loader)
        return self._replay_iter

    def eval(self):
        step, episode, total_reward = 0, 0, 0
        eval_until_episode = utils.Until(self.cfg.num_eval_episodes)

        while eval_until_episode(episode):
            time_step = self.eval_env.reset()
            self.video_recorder.init(self.eval_env, enabled=(episode == 0))
            while not time_step.last():
                with torch.no_grad(), utils.eval_mode(self.agent):
                    action = self.agent.act(time_step.observation,
                                            self.global_step,
                                            eval_mode=True)
                time_step = self.eval_env.step(action)
                self.video_recorder.record(self.eval_env)
                total_reward += time_step.reward
                step += 1

            episode += 1
            self.video_recorder.save(f'{self.global_frame}.mp4')

        with self.logger.log_and_dump_ctx(self.global_frame, ty='eval') as log:
            log('episode_reward', total_reward / episode)
            log('episode_length', step * self.cfg.action_repeat / episode)
            log('episode', self.global_episode)
            log('step', self.global_step)
            
    def habi_eval(self):
        step, episode, total_reward = 0, 0, 0
        eval_until_episode = utils.Until(self.cfg.num_eval_episodes)

        success_rate = 0
        while eval_until_episode(episode):
            time_step = self.eval_env.reset()
            self.video_recorder.init(self.eval_env, enabled=(episode == 0))
            while not time_step.last():
                with torch.no_grad(), utils.eval_mode(self.agent):
                    action = self.agent.act(time_step.observation,
                                            self.global_step,
                                            eval_mode=True)
                time_step = self.eval_env.step(action)
                self.video_recorder.record(self.eval_env)
                total_reward += time_step.reward
                step += 1

            episode += 1
            success_rate += time_step.info['success']
            self.video_recorder.save(f'{self.global_frame}.mp4')

        with self.logger.log_and_dump_ctx(self.global_frame, ty='eval') as log:
            log('episode_reward', total_reward / episode)
            log('episode_length', step * self.cfg.action_repeat / episode)
            log('episode', self.global_episode)
            log('step', self.global_step)
            log('success_rate', success_rate / episode)
        if self.cfg.save_snapshot:
            self.save_snapshot()

    def train(self):
        # predicates
        train_until_step = utils.Until(self.cfg.num_train_frames,
                                       self.cfg.action_repeat)
        seed_until_step = utils.Until(self.cfg.num_seed_frames,
                                      self.cfg.action_repeat)
        eval_every_step = utils.Every(self.cfg.eval_every_frames,
                                      self.cfg.action_repeat)

        episode_step, episode_reward = 0, 0
        time_step = self.train_env.reset()
        self.replay_storage.add(time_step)
        self.train_video_recorder.init(time_step.observation)
        metrics = None
        while train_until_step(self.global_step):
            if time_step.last():
                self._global_episode += 1
                self.train_video_recorder.save(f'{self.global_frame}.mp4')
                # wait until all the metrics schema is populated
                if metrics is not None:
                    # log stats
                    elapsed_time, total_time = self.timer.reset()
                    episode_frame = episode_step * self.cfg.action_repeat
                    with self.logger.log_and_dump_ctx(self.global_frame,
                                                      ty='train') as log:
                        log('fps', episode_frame / elapsed_time)
                        log('total_time', total_time)
                        log('episode_reward', episode_reward)
                        log('episode_length', episode_frame)
                        log('episode', self.global_episode)
                        log('buffer_size', len(self.replay_storage))
                        log('step', self.global_step)

                # reset env
                time_step = self.train_env.reset()
                self.replay_storage.add(time_step)
                self.train_video_recorder.init(time_step.observation)
                # try to save snapshot
                if self.cfg.save_snapshot and (self.global_step % int(5e4) == 0):
                    self.save_snapshot()
                episode_step = 0
                episode_reward = 0

            # try to evaluate
            if eval_every_step(self.global_step):
                self.logger.log('eval_total_time', self.timer.total_time(),
                                self.global_frame)
                if self.cfg.env == 'habitat':
                    self.habi_eval()
                else:
                    self.eval()

            # sample action
            with torch.no_grad(), utils.eval_mode(self.agent):
                action = self.agent.act(time_step.observation,
                                        self.global_step,
                                        eval_mode=False)

            # try to update the agent
            if not seed_until_step(self.global_step):
                metrics = self.agent.update(self.replay_iter, self.global_step)
                self.logger.log_metrics(metrics, self.global_frame, ty='train')

            # take env step
            time_step = self.train_env.step(action)
            episode_reward += time_step.reward
            self.replay_storage.add(time_step)
            self.train_video_recorder.record(time_step.observation)
            episode_step += 1
            self._global_step += 1

    def save_snapshot(self):
        snapshot = self.work_dir / 'snapshot.pt'
        keys_to_save = ['agent', 'timer', '_global_step', '_global_episode']
        payload = {k: self.__dict__[k] for k in keys_to_save}
        with snapshot.open('wb') as f:
            torch.save(payload, f)

    def load_snapshot(self):
        snapshot = self.work_dir / 'snapshot.pt'
        with snapshot.open('rb') as f:
            payload = torch.load(f)
        for k, v in payload.items():
            self.__dict__[k] = v


@hydra.main(config_path='cfgs', config_name='config')
def main(cfg):
    from train import Workspace as W
    root_dir = Path.cwd()
    workspace = W(cfg)
    snapshot = root_dir / 'snapshot.pt'
    if snapshot.exists():
        print(f'resuming: {snapshot}')
        workspace.load_snapshot()
    workspace.train()


if __name__ == '__main__':
    main()