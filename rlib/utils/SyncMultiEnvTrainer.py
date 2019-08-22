import time, datetime, os
import tensorflow as tf
import threading
import numpy as np
import copy
from abc import ABC, abstractmethod
from rlib.utils.utils import fold_batch



class SyncMultiEnvTrainer(object):
    def __init__(self, envs, model, file_loc, val_envs, train_mode='nstep', return_type='nstep', total_steps=10000, nsteps=5, gamma=0.99, lambda_=0.95, 
                     validate_freq=1e6, save_freq=0, render_freq=0, update_target_freq = 10000, num_val_episodes=50,
                     log_scalars=True):
        '''
            A synchronous multiple env training framework for tensorflow v.1 api 

            Args:
                envs - BatchEnv method which runs multiple environements synchronously 
                model - reinforcement learning model
                file_loc - as list of strings for saving model and tensorboard results in order of [model_directory, train_log_dir]
                val_envs - a list of envs for validation 
                train_mode - 'nstep' or 'onestep' species whether training is done using multiple step TD learning or single step 
                total_steps - number of Total training steps across all environements
                nsteps - number of steps TD error is caluclated over
                validate_freq - number of steps across all environements before performing validating, 0 for no validation 
                save_freq - number of steps across all environements before saving model, 0 for no saving 
                render_freq - multiple of validate_freq before rendering (i.e. render every X validations), 0 for no rendering
                update_target_freq - number of steps across all environements before updating target model, 0 for no updating
                num_val_episodes - number of episodes to average over when validating
        '''
        self.env = envs
        if train_mode not in ['nstep', 'onestep']:
            raise ValueError('train_mode %s is not a valid argument. Valid arguments are ... %s, %s' %(train_mode,'nstep','onestep'))
        assert num_val_episodes >= len(val_envs), 'number of validation epsiodes {} must be greater than or equal to the number of validation envs {}'.format(num_val_episodes, len(val_envs))
        if return_type.lower() not in ['nstep', 'lambda', 'gae']:
            raise ValueError('return_type %s is not a valid argument. Valid arguments are ... %s, %s, %s' %(return_type, 'nstep', 'lambda', 'gae'))
        self.train_mode = train_mode
        self.num_envs = len(envs)
        self.env_id = envs.spec.id
        self.val_envs = val_envs
        self.validate_rewards = []
        self.model = model

        config = tf.compat.v1.ConfigProto() #GPU 
        config.gpu_options.allow_growth=True #GPU
        #config.log_device_placement=True
        #config = tf.ConfigProto(device_count = {'GPU': 0}) #CPU ONLY
        self.sess = tf.compat.v1.Session(config=config)
        self.model.set_session(self.sess)
    
        self.total_steps = int(total_steps)
        self.nsteps = nsteps
        self.return_type = return_type.lower()
        self.gamma = gamma
        self.lambda_ = lambda_

        self.validate_freq = int(validate_freq / (self.num_envs*self.nsteps))
        self.num_val_episodes = num_val_episodes
        self.lock = threading.Lock()

        self.save_freq = save_freq//(self.num_envs*self.nsteps)
        self.render_freq = render_freq
        self.target_freq = int(update_target_freq/(self.num_envs*self.nsteps))
        self.log_scalars = log_scalars
        self.model_dir, train_log_dir = file_loc
        current_time = datetime.datetime.now().strftime("%y-%m-%d_%H-%M-%S")

        if log_scalars:
            # Tensorboard Variables
            train_log_dir = train_log_dir + current_time + '/train'
            
            tf_epLoss = tf.compat.v1.placeholder('float',name='epsiode_loss')
            tf_epReward =  tf.compat.v1.placeholder('float',name='episode_reward')
            self.tf_placeholders = (tf_epLoss,tf_epReward)

            tf_sum_epLoss = tf.compat.v1.summary.scalar('epsiode_loss', tf_epLoss)
            tf_sum_epReward = tf.compat.v1.summary.scalar('episode_reward', tf_epReward)
            self.tf_summary_scalars= (tf_sum_epLoss,tf_sum_epReward)
            
            self.train_writer = tf.compat.v1.summary.FileWriter(train_log_dir, self.sess.graph)

        
        self.saver = tf.train.Saver()
        init = tf.global_variables_initializer()
        self.sess.run(init)
        
        self.current_time = current_time

        if not os.path.exists(self.model_dir) and save_freq > 0:
            os.makedirs(self.model_dir)
    
    def __del__(self):
        self.env.close()
        

    def train(self):
        if self.train_mode == 'nstep':
            self._train_nstep()
        elif self.train_mode == 'onestep':
            self._train_onestep()
        else:
            raise ValueError('%s is not a valid training mode'%(self.train_mode))
    
    @abstractmethod
    def _train_nstep(self):
        '''
            template for multi-step training loop for synchronous training over multiple environments
        '''
        start = time.time()
        num_updates = self.total_steps // (self.num_envs * self.nsteps)
        s = 0
        # main loop
        for t in range(1,num_updates+1):
            states, actions, rewards, dones, infos, values, last_values = self.runner.run()
            if self.return_type == 'nstep':
                R = self.nstep_return(rewards, last_values, dones, gamma=self.gamma)
            elif self.return_type == 'gae':
                R = self.GAE(rewards, values, last_values, dones, gamma=self.gamma, lambda_=self.lambda_) + values
            elif self.return_type == 'lambda':
                R = self.lambda_return(rewards, values, last_values, dones, gamma=self.gamma, lambda_=self.lambda_, clip=False)
            # stack all states, actions and Rs from all workers into a single batch
            states, actions, R = fold_batch(states), fold_batch(actions), fold_batch(R)    
            l = self.model.backprop(states, R, actions)

            if self.render_freq > 0 and t % (self.validate_freq * self.render_freq) == 0:
                render = True
            else:
                render = False
     
            if self.validate_freq > 0 and t % self.validate_freq == 0:
                self.validation_summary(t,l,start,render)
                start = time.time()
            
            if self.save_freq > 0 and  t % self.save_freq == 0: 
                s += 1
                self.save_model(s)
                print('saved model')
            
            if self.target_freq > 0 and t % self.target_freq == 0: # update target network (for value based learning e.g. DQN)
                self.update_target()
    
    
    def nstep_return(self, rewards, last_values, dones, gamma=0.99, clip=False):
        if clip:
            rewards = np.clip(rewards, -1, 1)

        T = len(rewards)
        
        # Calculate R for advantage A = R - V 
        R = np.zeros_like(rewards)
        R[-1] = last_values * (1-dones[-1])
        
        for i in reversed(range(T-1)):
            # restart score if done as BatchEnv automatically resets after end of episode
            R[i] = rewards[i] + gamma * R[i+1] * (1-dones[i])
        
        return R
    
    def lambda_return(self, rewards, values, last_values, dones, gamma=0.99, lambda_=0.8, clip=False):
        if clip:
            rewards = np.clip(rewards, -1, 1)
        T = len(rewards)
        # Calculate eligibility trace R^lambda 
        R = np.zeros_like(rewards)
        R[-1] =  last_values * (1-dones[-1])
        for t in reversed(range(T-1)):
            # restart score if done as BatchEnv automatically resets after end of episode
            R[t] = rewards[t] + gamma * (lambda_* R[t+1] + (1.0-lambda_) * values[t+1]) * (1-dones[t])
        
        return R

    def GAE(self, rewards, values, last_values, dones, gamma=0.99, lambda_=0.95, clip=False):
        if clip:
            rewards = np.clip(rewards, -1, 1)
        # Generalised Advantage Estimation
        Adv = np.zeros_like(rewards)
        Adv[-1] = rewards[-1] + gamma * last_values * (1-dones[-1]) - values[-1]
        T = len(rewards)
        for t in reversed(range(T-1)):
            delta = rewards[t] + gamma * values[t+1] * (1-dones[t]) - values[t]
            Adv[t] = delta + gamma * lambda_ * Adv[t+1] * (1-dones[t])
        
        return Adv
    
    def validation_summary(self,t,loss,start,render):
        tot_steps = t * self.num_envs * self.nsteps
        time_taken = time.time() - start
        frames_per_update = (self.validate_freq * self.num_envs * self.nsteps)
        fps = frames_per_update /time_taken 
        num_val_envs = len(self.val_envs)
        num_val_eps = [self.num_val_episodes//num_val_envs for i in range(num_val_envs)]
        num_val_eps[-1] = num_val_eps[-1] + self.num_val_episodes % self.num_val_episodes//(num_val_envs)
        render_array = np.zeros((len(self.val_envs)))
        render_array[0] = render
        threads = [threading.Thread(daemon=True,target=self.validate, args=(self.val_envs[i], num_val_eps[i], 10000, render_array[i])) for i in range(num_val_envs)]
        
        try:
            for thread in threads:
                thread.start()
            
            for thread in threads:
                thread.join()
    
        except KeyboardInterrupt:
            for thread in threads:
                thread.join()
    
            
        score = np.mean(self.validate_rewards)
        self.validate_rewards = []
        print("update %i, validation score %f, total steps %i, loss %f, time taken for %i frames:%fs, fps %f" %(t,score,tot_steps,loss,frames_per_update,time_taken,fps))
        
        if self.log_scalars:
            tf_epLoss, tf_epScore, = self.tf_placeholders
            tf_sum_epLoss, tf_sum_epScore, = self.tf_summary_scalars
            sumscore, sumloss = self.sess.run([tf_sum_epScore, tf_sum_epLoss], feed_dict = {tf_epScore:score, tf_epLoss:loss})
            self.train_writer.add_summary(sumloss, tot_steps)
            self.train_writer.add_summary(sumscore, tot_steps)
    
    def save_model(self,s):
        model_loc = str(self.model_dir + self.current_time + '/' + str(s))
        # default saving method is to save session
        self.saver.save(self.sess, model_loc + ".ckpt")
    
    @abstractmethod
    def update_target(self):
        pass
        

    @abstractmethod
    def _train_onestep(self):
        ''' more efficient implementation of train_nstep when nsteps=1
        '''
        raise NotImplementedError(self, 'does not have an one-step training implementation')
    
    def save_hyperparameters(self, filename, **kwargs):
        handle = open(filename, "w")
        for key, value in kwargs.items():
            handle.write("{} = {}\n" .format(key, value))
        handle.close()

    def validate(self,env,num_ep,max_steps,render=False):
        episode_scores = []
        for episode in range(num_ep):
            state = env.reset()
            episode_score = []
            for t in range(max_steps):
                action = self.get_action(state[np.newaxis])
                next_state, reward, done, info = env.step(action)
                state = next_state
                #print('state', state, 'action', action, 'reward', reward)

                episode_score.append(reward)
                
                if render:
                    with self.lock:
                        env.render()

                if done or t == max_steps -1:
                    tot_reward = np.sum(episode_score)
                    with self.lock:
                        self.validate_rewards.append(tot_reward)
                    
                    break
        if render:
            with self.lock:
                env.close()
    
    def get_action(self,state): #include small fn as to reuse validate 
        raise NotImplementedError('get_action method is required, check that this is implemented properly')
    
    def fold_batch(self,x):
        rows, cols = x.shape[0], x.shape[1]
        y = x.reshape(rows*cols,*x.shape[2:])
        return y
            
    
    class Runner(ABC):
        def __init__(self,model,env,num_steps):
            self.model = model
            self.env = env
            self.num_steps = num_steps
            self.states = self.env.reset()
        
        @abstractmethod
        def run(self):
            pass



            
    
    
