"""
Wrappers for energyplus env.
1. Combine system timesteps into control timesteps. We compare whether ob[:3] is identical for
current step and previous step. We use the same action within the same control step (15 min).
"""

import os
import shutil

import gym.spaces
import gym.spaces as spaces
import numpy as np
import torch
from gym.core import Wrapper, ObservationWrapper, ActionWrapper
from torch.utils.tensorboard import SummaryWriter
from torchlib.common import convert_numpy_to_tensor
from torchlib.deep_rl.envs.model_based import ModelBasedEnv


class CostFnWrapper(Wrapper):
    def cost_fn(self, states, actions, next_states):
        return self.env.cost_fn(states, actions, next_states)


class RepeatAction(CostFnWrapper, ModelBasedEnv):
    def __init__(self, env):
        super(RepeatAction, self).__init__(env=env)
        self.last_obs = None
        self.reward = []
        self.obs = []

    def step(self, action):
        obs, reward, done, info = self.env.step(action)
        self.obs.append(obs)
        self.reward.append(reward)

        # repeat the same action until it is done or the obs is different.
        while np.array_equal(obs[:3], self.last_obs[:3]) and (not done):
            obs, reward, done, info = self.env.step(action)
            self.obs.append(obs)
            self.reward.append(reward)

        self.last_obs = obs

        obs = np.mean(self.obs, axis=0)
        reward = np.mean(self.reward)

        self.obs = []
        self.reward = []

        return obs, reward, done, info

    def reset(self, **kwargs):
        obs = self.env.reset(**kwargs)
        self.last_obs = obs
        return obs


class EnergyPlusWrapper(CostFnWrapper):
    """
    Break a super long episode env into small length episodes. Used for PPO
    1. If the user calls reset, it will remain at the originally step.
    2. If the user reaches the maximum length, return done.
    3. If the user touches the true done, yield done.
    """

    def __init__(self, env, max_steps=96 * 5):
        super(EnergyPlusWrapper, self).__init__(env=env)
        assert max_steps > 0, 'max_steps must be greater than zero. Got {}'.format(max_steps)
        self.max_steps = max_steps
        self.true_done = True
        self.last_obs = None

        self.action_space = gym.spaces.Box(low=-1., high=1., shape=self.env.action_space.low.shape)

    def step(self, action):
        assert self.action_space.contains(action), 'Action {} is out of bound of [-1, 1]'.format(action)

        obs, reward, done, info = self.env.step(action)
        self.last_obs = obs
        if done:
            self.true_done = True
            info['true_done'] = True
            return self.get_obs(), reward, done, info

        info['true_done'] = False
        self.current_steps += 1

        if self.current_steps == self.max_steps:
            return self.get_obs(), reward, True, info
        elif self.current_steps < self.max_steps:
            return self.get_obs(), reward, done, info
        else:
            raise ValueError('Please call reset before step.')

    def get_obs(self):
        return self.last_obs

    def reset(self, **kwargs):
        if self.true_done:
            self.last_obs = self.env.reset(**kwargs)
            self.true_done = False
        self.current_steps = 0
        return self.get_obs()


class Monitor(CostFnWrapper):
    def __init__(self, env, log_dir):
        super(Monitor, self).__init__(env=env)
        assert log_dir is not None, "log_dir can't be None"
        if os.path.isdir(log_dir):
            shutil.rmtree(log_dir)
        self.log_dir = log_dir
        self.writer = SummaryWriter(log_dir=log_dir)
        self.global_step = 0
        self.episode_index = 0

    def step(self, action):
        obs, reward, done, info = self.env.step(action)
        original_action = self.action_space.low + (action + 1.) * 0.5 * (self.action_space.high -
                                                                         self.action_space.low)
        self.dump_csv(obs, original_action, reward)
        self.dump_tensorboard(obs, original_action, reward)
        self.global_step += 1
        if done:
            self.logger.close()
        return obs, reward, done, info

    def reset(self, **kwargs):
        self.logger = open(os.path.join(self.log_dir, 'episode-{}.csv'.format(self.episode_index)), 'w')
        self.episode_index += 1
        self.dump_csv_header()
        return self.env.reset(**kwargs)

    def dump_csv_header(self):
        self.logger.write('outside_temperature,west_temperature,east_temperature,ite_power,hvac_power,' +
                          'west_setpoint,east_setpoint,west_airflow,east_airflow,reward\n')

    def dump_csv(self, obs, original_action, reward):
        self.logger.write('{:.2f},{:.2f},{:.2f},{:.2f},{:.2f},{:.2f},{:.2f},{:.2f},{:.2f},{:.4f}\n'.format(
            obs[0], obs[1], obs[2], obs[4], obs[5], original_action[0], original_action[1],
            original_action[2], original_action[3], reward))

    def dump_tensorboard(self, obs, original_action, reward):
        self.writer.add_scalar('observation/outside_temperature', obs[0], self.global_step)
        self.writer.add_scalar('observation/west_temperature', obs[1], self.global_step)
        self.writer.add_scalar('observation/east_temperature', obs[2], self.global_step)
        self.writer.add_scalar('observation/ite_power (MW)', obs[4] / 1e6, self.global_step)
        self.writer.add_scalar('observation/hvac_power (MW)', obs[5] / 1e6, self.global_step)
        self.writer.add_scalar('action/west_setpoint', original_action[0], self.global_step)
        self.writer.add_scalar('action/east_setpoint', original_action[1], self.global_step)
        self.writer.add_scalar('action/west_airflow', original_action[2], self.global_step)
        self.writer.add_scalar('action/east_airflow', original_action[3], self.global_step)
        self.writer.add_scalar('data/reward', reward, self.global_step)


class EnergyPlusObsWrapper(ObservationWrapper, CostFnWrapper):
    def __init__(self, env):
        super(EnergyPlusObsWrapper, self).__init__(env=env)
        self.obs_max = np.array([50., 50., 50., 1e5, 1e5], dtype=np.float32)
        self.obs_max_tensor = convert_numpy_to_tensor(self.obs_max).unsqueeze(dim=0)

        self.observation_space = spaces.Box(low=np.array([-20.0, -20.0, -20.0, 0.0, 0.0]),
                                            high=np.array([50.0, 50.0, 50.0, 1000000000.0, 1000000000.0]),
                                            dtype=np.float32)

    def reverse_observation(self, normalized_obs):
        obs = normalized_obs * self.obs_max
        total_power = obs[3] + obs[4]
        obs = np.insert(obs, 3, total_power)
        return obs

    def observation(self, observation):
        temperature_obs = observation[0:3]
        power_obs = observation[4:]
        obs = np.concatenate((temperature_obs, power_obs))
        return obs / self.obs_max

    def reverse_observation_batch_tensor(self, normalized_obs):
        assert isinstance(normalized_obs, torch.Tensor)
        obs = normalized_obs * self.obs_max_tensor
        total_power = obs[:, 3:4] + obs[:, 4:5]
        obs = torch.cat((obs[:, :3], total_power, obs[:, 3:]), dim=-1)
        return obs

    def cost_fn(self, states, actions, next_states):
        states = self.reverse_observation_batch_tensor(states)
        next_states = self.reverse_observation_batch_tensor(next_states)
        return self.env.cost_fn(states, actions, next_states)


class EnergyPlusDiscreteActionWrapper(ActionWrapper):
    def __init__(self, env, num_levels=4):
        super(EnergyPlusDiscreteActionWrapper, self).__init__(env=env)
        self.action_space = spaces.Discrete(num_levels ** env.action_space.shape[0])
        self.action_table = np.linspace(-1., 1., num_levels)
        self.num_levels = num_levels

    def action(self, action):
        """

        Args:
            action: a integer ranging from 0 to max

        Returns: n * [-1, 1]

        """
        assert self.action_space.contains(action), 'Action {} is not in space {}'.format(
            action, self.action_space)
        binary_action = []
        for _ in range(self.env.action_space.shape[0]):
            remainder = action % self.num_levels
            binary_action.append(remainder)
            action = (action - remainder) // self.num_levels
        action = self.action_table[binary_action]
        return action

    def reverse_action(self, action):
        """ Find the closest action in action_table and translate to MultiDiscrete.
            Then translate to Discrete

        Args:
            action:

        Returns:

        """
        pass