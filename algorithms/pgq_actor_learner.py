# -*- coding: utf-8 -*-
import time
import numpy as np
import utils.logger
import tensorflow as tf
from actor_learner import ONE_LIFE_GAMES
from utils.decorators import Experimental
from utils.replay_memory import ReplayMemory
from networks.policy_v_network import PolicyValueNetwork
from policy_based_actor_learner import BaseA3CLearner


logger = utils.logger.getLogger('pgq_actor_learner')


class BasePGQLearner(BaseA3CLearner):
    def __init__(self, args):

        super(BasePGQLearner, self).__init__(args)

        self.replay_size = args.replay_size
        self.pgq_fraction = args.pgq_fraction
        self.batch_update_size = args.batch_update_size
        scope_name = 'local_learning_{}'.format(self.actor_id)
        conf_learning = {'name': scope_name,
                         'input_shape': self.input_shape,
                         'num_act': self.num_actions,
                         'args': args}

        with tf.device('/cpu:0'):
            self.local_network = PolicyValueNetwork(conf_learning)
        with tf.device('/gpu:0'), tf.variable_scope('', reuse=True):
            self.batch_network = PolicyValueNetwork(conf_learning)
            self._build_q_ops()

        self.reset_hidden_state()
            
        if self.is_master():
            var_list = self.local_network.params
            self.saver = tf.train.Saver(var_list=var_list, max_to_keep=3, 
                                        keep_checkpoint_every_n_hours=2)


    def _build_q_ops(self):
        # pgq specific initialization
        self.pgq_fraction = self.pgq_fraction
        self.batch_size = self.batch_update_size
        self.replay_memory = ReplayMemory(self.replay_size)
        self.q_tilde = self.batch_network.beta * (
            self.batch_network.log_output_layer_pi
            + tf.expand_dims(self.batch_network.output_layer_entropy, 1)
        ) + self.batch_network.output_layer_v

        self.Qi, self.Qi_plus_1 = tf.split(axis=0, num_or_size_splits=2, value=self.q_tilde)
        self.V, _ = tf.split(axis=0, num_or_size_splits=2, value=self.batch_network.output_layer_v)
        self.log_pi, _ = tf.split(axis=0, num_or_size_splits=2, value=tf.expand_dims(self.batch_network.log_output_selected_action, 1))
        self.R = tf.placeholder('float32', [None], name='1-step_reward')

        self.terminal_indicator = tf.placeholder(tf.float32, [None], name='terminal_indicator')
        self.max_TQ = self.gamma*tf.reduce_max(self.Qi_plus_1, 1) * (1 - self.terminal_indicator)
        self.Q_a = tf.reduce_sum(self.Qi * tf.split(axis=0, num_or_size_splits=2, value=self.batch_network.selected_action_ph)[0], 1)

        self.q_objective = - self.pgq_fraction * tf.reduce_mean(tf.stop_gradient(self.R + self.max_TQ - self.Q_a) * (self.V[:, 0] + self.log_pi[:, 0]))

        self.V_params = self.batch_network.params
        self.q_gradients = tf.gradients(self.q_objective, self.V_params)

        if self.batch_network.clip_norm_type == 'global':
            self.q_gradients = tf.clip_by_global_norm(
                self.q_gradients, self.batch_network.clip_norm)[0]
        elif self.batch_network.clip_norm_type == 'local':
            self.q_gradients = [tf.clip_by_norm(
                g, self.batch_network.clip_norm) for g in self.q_gradients]

        # if (self.optimizer_mode == "local"):
        #     if (self.optimizer_type == "rmsprop"):
        #         self.batch_opt_st = np.ones(size, dtype=ctypes.c_float)
        #     else:
        #         self.batch_opt_st = np.zeros(size, dtype=ctypes.c_float)
        # elif (self.optimizer_mode == "shared"):
        #         self.batch_opt_st = args.batch_opt_state


    def apply_batch_q_update(self):
        if len(self.replay_memory) < self.replay_memory.maxlen//10:
            return

        s_i, a_i, r_i, s_f, is_terminal = self.replay_memory.sample_batch(self.batch_size)

        batch_grads, max_TQ, Q_a = self.session.run(
            [self.q_gradients, self.max_TQ, self.Q_a],
            feed_dict={
                self.R: r_i,
                self.batch_network.selected_action_ph: np.vstack([a_i, a_i]),
                self.batch_network.input_ph: np.vstack([s_i, s_f]),
                self.terminal_indicator: is_terminal.astype(np.int),
            }
        )
        self.apply_gradients_to_shared_memory_vars(batch_grads)


class PGQLearner(BasePGQLearner):
    def choose_next_action(self, state):
        network_output_v, network_output_pi = self.session.run(
                [self.local_network.output_layer_v,
                 self.local_network.output_layer_pi], 
                feed_dict={self.local_network.input_ph: [state]})

        network_output_pi = network_output_pi.reshape(-1)
        network_output_v = np.asscalar(network_output_v)

        action_index = self.sample_policy_action(network_output_pi)
        new_action = np.zeros([self.num_actions])
        new_action[action_index] = 1

        return new_action, network_output_v, network_output_pi


    def train(self):
        """ Main actor learner loop for advantage actor critic learning. """
        logger.debug("Actor {} resuming at Step {}".format(self.actor_id, 
            self.global_step.value()))

        s = self.emulator.get_initial_state()
        steps_at_last_reward = self.local_step
        total_episode_reward = 0.0
        mean_entropy = 0.0
        q_update_counter = 0
        episode_start_step = 0
        
        while (self.global_step.value() < self.max_global_steps):
            # Sync local learning net with shared mem
            self.sync_net_with_shared_memory(self.local_network, self.learning_vars)
            self.save_vars()

            local_step_start = self.local_step
            reset_game = False
            episode_over = False
            
            states    = list()
            rewards   = list()
            actions   = list()
            values    = list()
            s_batch   = list()
            a_batch   = list()
            y_batch   = list()
            adv_batch = list()
            
            while not (episode_over 
                or (self.local_step - local_step_start 
                    == self.max_local_steps)):
                
                # Choose next action and execute it
                a, readout_v_t, readout_pi_t = self.choose_next_action(s)

                if self.is_master() and (self.local_step % 100 == 0):
                    logger.debug("pi={}, V={}".format(readout_pi_t, readout_v_t))
                    
                new_s, reward, episode_over = self.emulator.next(a)

                if reward != 0.0:
                    steps_at_last_reward = self.local_step


                total_episode_reward += reward
                # Rescale or clip immediate reward
                reward = self.rescale_reward(reward)
                self.replay_memory.append(s, a, reward, episode_over)
                
                rewards.append(reward)
                states.append(s)
                actions.append(a)
                values.append(readout_v_t)
                
                s = new_s
                self.local_step += 1
                self.global_step.increment()
                
            
            # Calculate the value offered by critic in the new state.
            if episode_over:
                R = 0
            else:
                R = self.session.run(
                    self.local_network.output_layer_v,
                    feed_dict={self.local_network.input_ph:[new_s]})[0][0]
                            
             
            sel_actions = []
            for i in reversed(xrange(len(states))):
                R = rewards[i] + self.gamma * R

                y_batch.append(R)
                a_batch.append(actions[i])
                s_batch.append(states[i])
                adv_batch.append(R - values[i])
                
                sel_actions.append(np.argmax(actions[i]))
                

            # Compute gradients on the local policy/V network and apply them to shared memory  
            feed_dict={
                self.local_network.input_ph: s_batch, 
                self.local_network.critic_target_ph: y_batch,
                self.local_network.selected_action_ph: a_batch,
                self.local_network.adv_actor_ph: adv_batch,
            }


            grads, entropy = self.session.run(
                [self.local_network.get_gradients, self.local_network.entropy],
                feed_dict=feed_dict)

            self.apply_gradients_to_shared_memory_vars(grads)

            q_update_counter += 1
            if q_update_counter % self.q_update_interval == 0:
                self.apply_batch_q_update()

            # policy_grads, entropy = self.session.run(
            #     [self.local_network.get_gradients, self.local_network.entropy],
            #     feed_dict=feed_dict)
            # q_grads = self.apply_batch_q_update()

            # grads = [p + q for p, q in zip(policy_grads, q_grads)]
            # self.apply_gradients_to_shared_memory_vars(grads)

            delta_old = local_step_start - episode_start_step
            delta_new = self.local_step -  local_step_start
            mean_entropy = (mean_entropy*delta_old + entropy*delta_new) / (delta_old + delta_new)
            
            s, mean_entropy, mean_value, episode_start_step, total_episode_reward, steps_at_last_reward = self.prepare_state(
                s, mean_entropy, np.array(values).mean(), episode_start_step, total_episode_reward, steps_at_last_reward, sel_actions, episode_over)


@Experimental
class PGQLSTMLearner(BasePGQLearner):
    '''
    Ordinary experience replay isn't quite compatible with the lstm network architecture,
    since the hidden states will quickly get stale. As we backprop through more timesteps
    the stale initial state will have less impact but that could make the q-updates quite
    expensive to compute. A possible alternative could be to incorporate some notion of
    "Queue Maintenance Workers" that continually forward-prop each episode in the replay 
    buffer to keep the hidden states fresh.
    '''
    def reset_hidden_state(self):
        self.lstm_state_out = np.zeros([1, 2*self.local_network.hidden_state_size])


    def choose_next_action(self, state):
        network_output_v, network_output_pi, self.lstm_state_out = self.session.run(
            [
                self.local_network.output_layer_v,
                self.local_network.output_layer_pi,
                self.local_network.lstm_state,
            ],
            feed_dict={
                self.local_network.input_ph: [state],
                self.local_network.step_size: [1],
                self.local_network.initial_lstm_state: self.lstm_state_out,
            })

        network_output_pi = network_output_pi.reshape(-1)
        network_output_v = np.asscalar(network_output_v)
        
        action_index = self.sample_policy_action(network_output_pi)
        new_action = np.zeros([self.num_actions])
        new_action[action_index] = 1

        return new_action, network_output_v, network_output_pi


    def apply_batch_q_update(self):
        s_i, lstm_state_i, a_i, r_i, s_f, lstm_state_f, is_terminal = \
            self.replay_memory.sample_batch(self.batch_size)


        batch_grads, max_TQ, Q_a = self.session.run(
            [self.q_gradients, self.max_TQ, self.Q_a],
            feed_dict={
                self.R: r_i,
                self.batch_network.selected_action_ph: np.vstack([a_i, a_i]),
                self.batch_network.input_ph: np.vstack([s_i, s_f]),
                self.batch_network.initial_lstm_state: np.vstack([lstm_state_i, lstm_state_f]),
                self.terminal_indicator: is_terminal.astype(np.int),
                self.batch_network.step_size: np.ones(2*len(s_i)),
            }
        )
        # print 'max_TQ={}, Q_a={}'.format(max_TQ[:5], Q_a[:5])

        # self._apply_gradients_to_shared_memory_vars(batch_grads, opt_st=self.batch_opt_st)
        self.apply_gradients_to_shared_memory_vars(batch_grads)


    def train(self):
        """ Main actor learner loop for advantage actor critic learning. """
        logger.debug("Actor {} resuming at Step {}".format(self.actor_id, 
            self.global_step.value()))

        s = self.emulator.get_initial_state()
        steps_at_last_reward = self.local_step
        total_episode_reward = 0.0
        mean_entropy = 0.0
        mean_value = 0.0
        q_update_counter = 0
        episode_start_step = 0
        
        while (self.global_step.value() < self.max_global_steps):
            # Sync local learning net with shared mem
            self.sync_net_with_shared_memory(self.local_network, self.learning_vars)
            self.save_vars()

            local_step_start = self.local_step
            local_lstm_state = np.copy(self.lstm_state_out)
            reset_game = False
            episode_over = False
            
            rewards   = list()
            states    = list()
            actions   = list()
            values    = list()
            s_batch   = list()
            a_batch   = list()
            y_batch   = list()
            adv_batch = list()

            while not (episode_over 
                or (self.local_step - local_step_start 
                    == self.max_local_steps)):
                
                # Choose next action and execute it
                previous_lstm_state = np.copy(self.lstm_state_out)
                a, readout_v_t, readout_pi_t = self.choose_next_action(s)
                
                delta = self.local_step - episode_start_step
                mean_value = (delta*mean_value + readout_v_t) / (1+delta)
                
                new_lstm_state = np.copy(self.lstm_state_out)
                assert not np.allclose(local_lstm_state, self.lstm_state_out)

                if self.is_master() and (self.local_step % 100 == 0):
                    logger.debug("pi={}, V={}".format(readout_pi_t, readout_v_t))
                    
                new_s, reward, episode_over = self.emulator.next(a)
                if reward != 0.0:
                    steps_at_last_reward = self.local_step


                total_episode_reward += reward
                # Rescale or clip immediate reward
                reward = self.rescale_reward(reward)
                self.replay_memory.append(
                    s, previous_lstm_state[0],
                    a,
                    reward,
                    episode_over)
                
                rewards.append(reward)
                states.append(s)
                actions.append(a)
                values.append(readout_v_t)
                
                s = new_s
                self.local_step += 1
                self.global_step.increment()
                
            
            # Calculate the value offered by critic in the new state.
            if episode_over:
                R = 0
            else:
                # compute with repsect to target network
                prev_lstm_state_out = self.lstm_state_out
                R = self.session.run(
                    self.local_network.output_layer_v,
                    feed_dict={
                        self.local_network.input_ph:[new_s],
                        self.local_network.step_size: [1],
                        self.local_network.initial_lstm_state: self.lstm_state_out,
                    }
                )[0][0]
                assert np.allclose(prev_lstm_state_out, self.lstm_state_out)
                            
             
            sel_actions = []
            for i in reversed(xrange(len(states))):
                R = rewards[i] + self.gamma * R

                y_batch.append(R)
                a_batch.append(actions[i])
                s_batch.append(states[i])
                adv_batch.append(R - values[i])
                
                sel_actions.append(np.argmax(actions[i]))
                
            # reverse everything so that the LSTM inputs are time-ordered
            y_batch.reverse()
            a_batch.reverse()
            s_batch.reverse()
            adv_batch.reverse()
            sel_actions.reverse()

            # Compute gradients on the local policy/V network and apply them to shared memory  
            feed_dict={
                self.local_network.input_ph: s_batch, 
                self.local_network.critic_target_ph: y_batch,
                self.local_network.selected_action_ph: a_batch,
                self.local_network.adv_actor_ph: adv_batch,
                self.local_network.step_size : [len(s_batch)],
                self.local_network.initial_lstm_state: local_lstm_state,
            }
            grads, entropy = self.session.run(
                [self.local_network.get_gradients, self.local_network.entropy],
                feed_dict=feed_dict)

            self.apply_gradients_to_shared_memory_vars(grads)

            q_update_counter += 1
            if q_update_counter % 4 == 0:
                self.apply_batch_q_update()

            delta_old = local_step_start - episode_start_step
            delta_new = self.local_step -  local_step_start
            mean_entropy = (mean_entropy*delta_old + entropy*delta_new) / (delta_old + delta_new)  
            
            s, mean_entropy, mean_value, episode_start_step, total_episode_reward, steps_at_last_reward = self.prepare_state(
                s, mean_entropy, mean_value, episode_start_step, total_episode_reward, steps_at_last_reward, sel_actions, episode_over)


